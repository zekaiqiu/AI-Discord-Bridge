"""Chat-side multi-account routing.

Counterpart of ``Multi-Agent-Framework/src/pipeline/account_pool.py`` but
scoped to what the chat service needs: pick an account at session-create
time and stick with it for the session's lifetime. We do NOT mid-session
fallover here — claude's ``--resume <uuid>`` requires the session UUID to
exist on the same account that originally created it, so silently
re-routing a follow-up turn to a different account would break
conversation continuity. Sessions are locked per-account at creation.

Two reasons we don't share account_pool.py with the framework directly:

  * Deployment shape — the pipeline runs as a host systemd-user process;
    the chat service runs as a Docker container with its own Python
    interpreter and dependency set. A shared module would require either
    a bind-mount of the framework into the container's PYTHONPATH (couples
    the two repos' deploy lifecycles) or a published library.

  * Scope — the framework's pool handles rate-limit fallover and a
    cooldown set; chat doesn't need that, only the discovery + pick
    primitives. Re-implementing those here is ~80 lines and keeps the
    chat service self-contained.

When the next ``!agent project`` of similar shape ships and we extract a
real shared library, both implementations can collapse onto it.

Discovery
=========
- ``main`` — bridge user's primary at ``~/.claude/.credentials.json``.
- ``<name>`` — every readable subdirectory under
  ``/opt/wizerith/claude-accounts/`` whose ``.claude/.credentials.json``
  exists.

Pick
====
``pick(min_headroom_pct=20.0)`` returns ``AccountChoice(name, home_path)``.
``home_path`` is what we set as ``HOME`` for the claude subprocess: it's
the directory containing ``.claude/.credentials.json`` (so claude reads
the right file). For ``main`` that's ``~``; for wizerith accounts it's
``/opt/wizerith/claude-accounts/<name>``.

Saturation cutoff (per window): the fast ``five_hour`` window retires at
``100 - min_headroom_pct`` (default 20 → ≥80%), gracefully retiring an
account before it crosses the orchestrator's historic 80% pause line. The
slow weekly ``seven_day*`` windows retire only at a full 100% — a 7d window
is a week-long budget, so excluding an account the moment it crosses 80%
strands up to a fifth of the weekly plan and locks the account out for days
even though every call there is still free plan quota.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("chat.account_router")


# ---------------------------------------------------------------------------
# Paths + tuning
# ---------------------------------------------------------------------------

# Inside the chat container the bind-mount maps /opt/wizerith/claude-accounts
# from the host to the same path inside the container. Override via env var
# for tests.
WIZERITH_ACCOUNTS_ROOT = Path(
    os.environ.get("WIZERITH_ACCOUNTS_ROOT", "/opt/wizerith/claude-accounts")
)

# The bridge user's ~/.claude. Inside the container, $HOME is set to
# /home/felix (per docker-compose env). Override via env var for tests.
MAIN_HOME = Path(os.environ.get("CHAT_MAIN_HOME", str(Path.home())))
MAIN_CREDENTIALS = MAIN_HOME / ".claude" / ".credentials.json"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_BETA = "oauth-2025-04-20"
USAGE_FETCH_TIMEOUT_SEC = 10.0
USAGE_CACHE_TTL_SEC = 60.0

# Windows we evaluate for saturation. Same set as the pipeline's pool, but
# split by retire policy: the fast 5h window retires at the headroom cutoff
# (default 80%), while the slow weekly (7d) windows retire only at a *full*
# 100% — a 7d window is a week-long budget, so excluding an account the moment
# it crosses 80% strands up to a fifth of the weekly plan and locks the account
# out for days even though every call would still be free plan quota.
_FIVE_HOUR_KEY = "five_hour"
_SEVEN_DAY_KEYS = (
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "seven_day_cowork",
    "seven_day_omelette",
    "seven_day_oauth_apps",
)
# Every window we read for ranking (lowest peak = freshest = preferred).
_QUOTA_WINDOW_KEYS = (_FIVE_HOUR_KEY, *_SEVEN_DAY_KEYS)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class NoAccountsAvailable(RuntimeError):
    """Raised when ``pick()`` finds zero candidate accounts on disk OR
    every account is at or above the saturation threshold. The caller
    should fall back to ``main`` (felix's primary) and let the user see
    the rate-limit through the normal stream-error path."""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountChoice:
    name: str
    # Directory to set as HOME when spawning claude. claude reads
    # ``$HOME/.claude/.credentials.json``.
    home_path: Path


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def list_accounts() -> list[tuple[str, Path]]:
    """Return [(name, home_path), ...] for accounts the chat service can see.

    Discovery only checks for the existence of ``<home>/.claude/.credentials.json``
    — readability of the file itself is checked when ``pick()`` actually
    needs to query usage. This way the discovery path can never block on a
    permission error.
    """
    # Accounts the router must never *pick* for new sessions. Default
    # excludes ``main`` — the bridge user's primary plan should not be
    # billed by web-chat sessions (the HOME=/home/felix watchdog treats
    # any such use as a leak). Existing sessions already locked to main
    # still resume there via home_for_account(); this only gates new
    # session creation. Override with CHAT_ACCOUNT_EXCLUDE="" to re-allow
    # main, or a comma-separated list to exclude others.
    excluded = {
        name.strip()
        for name in os.environ.get("CHAT_ACCOUNT_EXCLUDE", "main").split(",")
        if name.strip()
    }
    accounts: list[tuple[str, Path]] = []
    if MAIN_CREDENTIALS.is_file() and "main" not in excluded:
        accounts.append(("main", MAIN_HOME))
    if WIZERITH_ACCOUNTS_ROOT.is_dir():
        try:
            for entry in sorted(WIZERITH_ACCOUNTS_ROOT.iterdir()):
                if not entry.is_dir() or not entry.name:
                    continue
                # Reserve `_`/`.` prefixes for sibling sub-pools (e.g. the
                # wizerith.ai stack's pool living inside the same parent
                # tree). These are not billable accounts; never enumerate.
                if entry.name[0] in ("_", "."):
                    continue
                if entry.name in excluded:
                    continue
                if (entry / ".claude" / ".credentials.json").is_file():
                    accounts.append((entry.name, entry))
        except (PermissionError, OSError) as exc:
            log.warning("listing %s failed: %s", WIZERITH_ACCOUNTS_ROOT, exc)
    return accounts


# ---------------------------------------------------------------------------
# Usage cache
# ---------------------------------------------------------------------------

_lock = threading.Lock()
# name -> (snapshot_dict, monotonic_ts_at_fetch)
_usage_cache: dict[str, tuple[dict[str, Any], float]] = {}

# Live-429 cooldown. The /api/oauth/usage dashboard LAGS the real limiter —
# it reports 0% utilization while /v1/messages already returns 429 — so the
# snapshot-based saturation check below cannot detect a genuinely rate-limited
# account. The only reliable signal is an OBSERVED 429 on a real turn: the
# dispatch path calls mark_hot(name, resets_at) and the account is excluded
# from is_usable()/pick() until it recovers. name -> wall-clock epoch deadline.
_hot_until: dict[str, float] = {}

# Fallback cooldown when an observed 429 carries no reset time, and a hard cap
# so a malformed remote resets_at can never park an account indefinitely.
RATE_LIMIT_DEFAULT_COOLDOWN_SEC = 300.0
RATE_LIMIT_MAX_COOLDOWN_SEC = 8 * 24 * 3600.0


def _read_token(creds_path: Path) -> str | None:
    """Read the access token out of one .credentials.json. None on any error."""
    try:
        with open(creds_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        oauth = data.get("claudeAiOauth") or {}
        return oauth.get("accessToken") or None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("reading %s failed: %s", creds_path, exc)
        return None


def _fetch_usage(access_token: str) -> dict[str, Any] | None:
    """Hit /api/oauth/usage with one access token. None on any error."""
    if not access_token:
        return None
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": ANTHROPIC_BETA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=USAGE_FETCH_TIMEOUT_SEC) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        log.warning("usage fetch failed: %s", exc)
        return None


def snapshot_usage(name: str, home_path: Path, *, force_refresh: bool = False) -> dict[str, Any] | None:
    """Return the /api/oauth/usage body for ``name``, cached for
    USAGE_CACHE_TTL_SEC. None when the credentials file isn't readable
    AND there is no cached entry to fall back on.

    Stale-cache fallback: if a fresh fetch fails (rate limit, transient
    network error, Anthropic outage) but we have a previously successful
    snapshot in `_usage_cache` for this account, return that stale value
    instead of None. Routing prefers stale-but-real data over treating
    the account as saturated, since the alternative — failing closed
    when *every* account is rate-limited at the same moment — collapses
    the entire provisioning pipeline. Worst case we route to a slightly
    outdated peak number; the in-session quota error from claude itself
    is still a clearer UX than "no provisioned per-user container".
    """
    now = time.monotonic()
    if not force_refresh:
        with _lock:
            cached = _usage_cache.get(name)
        if cached is not None:
            snap, fetched_at = cached
            if now - fetched_at < USAGE_CACHE_TTL_SEC:
                return snap

    creds_path = home_path / ".claude" / ".credentials.json"
    token = _read_token(creds_path)
    if token is None:
        return None
    snap = _fetch_usage(token)
    if snap is None:
        with _lock:
            cached = _usage_cache.get(name)
        if cached is not None:
            log.info("router %s: fetch failed, using stale cache", name)
            return cached[0]
        return None
    with _lock:
        _usage_cache[name] = (snap, now)
    return snap


def _peak_utilization(snapshot: dict[str, Any] | None) -> float:
    if not snapshot:
        return 0.0
    peak = 0.0
    for key in _QUOTA_WINDOW_KEYS:
        win = snapshot.get(key)
        if not isinstance(win, dict):
            continue
        util = win.get("utilization")
        if isinstance(util, (int, float)) and util > peak:
            peak = float(util)
    return peak


def _is_plan_saturated(
    snapshot: dict[str, Any] | None, saturation_cutoff: float
) -> bool:
    """True when the account has no usable plan headroom left.

    Per-window thresholds: ``five_hour`` retires at ``saturation_cutoff``
    (default 80%, leaving the 20% margin for in-flight calls), while the
    weekly ``seven_day*`` windows retire only at a full 100%. A 7d window in
    the 80–99% band is NOT saturated — it refills weekly and every call there
    is still free plan quota, so retiring it early just strands plan budget.
    """
    if not isinstance(snapshot, dict):
        return False
    five = snapshot.get(_FIVE_HOUR_KEY)
    if isinstance(five, dict):
        util = five.get("utilization")
        if isinstance(util, (int, float)) and util >= saturation_cutoff:
            return True
    for key in _SEVEN_DAY_KEYS:
        win = snapshot.get(key)
        if not isinstance(win, dict):
            continue
        util = win.get("utilization")
        if isinstance(util, (int, float)) and util >= 100.0:
            return True
    return False


# ---------------------------------------------------------------------------
# Live-429 cooldown (observed-failure signal; authoritative over the dashboard)
# ---------------------------------------------------------------------------

def mark_hot(name: str, until_ts: float | None = None) -> None:
    """Park ``name`` in the rate-limit cooldown until wall-clock ``until_ts``
    (epoch seconds). Called when a turn actually observes a 429 for this
    account — the authoritative saturation signal, since /api/oauth/usage lags
    the live limiter and reports headroom that doesn't exist. ``until_ts`` that
    is None or already in the past falls back to RATE_LIMIT_DEFAULT_COOLDOWN_SEC;
    every value is capped at RATE_LIMIT_MAX_COOLDOWN_SEC so a bad remote
    ``resets_at`` can't lock an account out forever. Idempotent — keeps the
    farther-out deadline."""
    now = time.time()
    if not isinstance(until_ts, (int, float)) or until_ts <= now:
        until_ts = now + RATE_LIMIT_DEFAULT_COOLDOWN_SEC
    until_ts = min(float(until_ts), now + RATE_LIMIT_MAX_COOLDOWN_SEC)
    with _lock:
        if until_ts > _hot_until.get(name, 0.0):
            _hot_until[name] = until_ts
            log.info(
                "router: account %s parked hot until %s (observed 429)",
                name, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until_ts)),
            )


def note_success(name: str) -> None:
    """Clear any cooldown on ``name`` — a turn just succeeded, proving it
    recovered ahead of the recorded reset (5h window refilled, or the cooldown
    was a default guess). Lets recovery be immediate rather than waiting out a
    possibly-conservative deadline."""
    with _lock:
        if _hot_until.pop(name, None) is not None:
            log.info("router: account %s cooldown cleared (turn succeeded)", name)


def _is_hot(name: str, now: float | None = None) -> bool:
    """True iff ``name`` is in the live-429 cooldown as of ``now``. Expired
    entries are evicted on read so the set self-cleans without a sweeper."""
    if now is None:
        now = time.time()
    with _lock:
        until = _hot_until.get(name)
        if until is None:
            return False
        if now >= until:
            del _hot_until[name]
            return False
        return True


def is_usable(name: str, *, min_headroom_pct: float = 20.0) -> bool:
    """True when ``name`` can serve new turns right now.

    Two layers, mirroring what actually breaks a turn:
      * token-level — the credential file resolves (via ``home_for_account``,
        which ignores the pick-time exclude filter), parses, and its access
        token is not expired. A dead token means guaranteed 401s.
      * plan-level — the same per-window saturation policy ``pick()``
        applies (5h ≥ cutoff, any 7d ≥ 100%).

    A missing usage snapshot (quota API 429/outage) does NOT disqualify:
    the token-level check already caught dead accounts, and failing closed
    on a transient quota-API error would wedge every container at once.
    """
    home = home_for_account(name)
    creds = home / ".claude" / ".credentials.json"
    try:
        with open(creds, "r", encoding="utf-8") as f:
            oauth = json.load(f).get("claudeAiOauth") or {}
    except (OSError, json.JSONDecodeError):
        return False
    if not oauth.get("accessToken"):
        return False
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at / 1000.0 <= time.time():
        return False
    # Observed-429 cooldown beats the (lagging) usage dashboard: an account
    # that just rate-limited a real turn is not usable even if /oauth/usage
    # still shows headroom.
    if _is_hot(name):
        return False
    snap = snapshot_usage(name, home)
    if snap is None:
        return True
    return not _is_plan_saturated(snap, 100.0 - min_headroom_pct)


# ---------------------------------------------------------------------------
# Pick
# ---------------------------------------------------------------------------

def pick(*, min_headroom_pct: float = 20.0) -> AccountChoice:
    """Return the account with the most headroom on its hottest window.

    An account is excluded when it is plan-saturated: its 5h window is ≥ the
    cutoff (default ≥80%, leaving room for in-flight calls to finish on the
    retiring account) OR any 7d window is fully consumed at ≥100%. Raises
    ``NoAccountsAvailable`` if every candidate is saturated or none are
    discoverable. The caller is expected to fall back to running with
    HOME=~/.claude (the main account) and let the user see the rate-
    limit naturally if that account is also tight.
    """
    saturation_cutoff = 100.0 - min_headroom_pct
    candidates = list_accounts()
    if not candidates:
        raise NoAccountsAvailable("no accounts on disk")

    scored: list[tuple[str, Path, float]] = []
    snapshot_unavailable: list[tuple[str, Path]] = []
    now = time.time()
    for name, home_path in candidates:
        # Observed-429 cooldown excludes the account regardless of what the
        # lagging usage dashboard reports — and keeps it out of the fail-open
        # path below (it's never added to snapshot_unavailable).
        if _is_hot(name, now):
            log.info("router skip %s: live rate-limit cooldown", name)
            continue
        snap = snapshot_usage(name, home_path)
        if snap is None:
            log.info("router skip %s: usage snapshot unavailable", name)
            snapshot_unavailable.append((name, home_path))
            continue
        if _is_plan_saturated(snap, saturation_cutoff):
            log.info(
                "router skip %s: plan-saturated (peak %.1f%%, 5h cutoff "
                "%.1f%% / 7d cutoff 100%%)",
                name, _peak_utilization(snap), saturation_cutoff,
            )
            continue
        scored.append((name, home_path, _peak_utilization(snap)))

    if scored:
        scored.sort(key=lambda t: (t[2], t[0]))
        name, home_path, _ = scored[0]
        return AccountChoice(name=name, home_path=home_path)

    # Nothing scored. Distinguish "every account is genuinely saturated"
    # from "we couldn't observe utilization for any account" — the second
    # is recoverable by falling back to a candidate with a readable token.
    # Without this, term-router (which sees only wizerith accounts) drops
    # all new-user provisioning whenever Anthropic's quota API rate-limits
    # us, even if the underlying account has plenty of headroom.
    if snapshot_unavailable and len(snapshot_unavailable) == len(candidates):
        for name, home_path in snapshot_unavailable:
            creds_path = home_path / ".claude" / ".credentials.json"
            if not creds_path.is_file():
                continue
            # "Readable token" must mean a token that can still work: the
            # expired account-2 was picked on every fail-open for days.
            try:
                with open(creds_path, "r", encoding="utf-8") as fh:
                    _exp = (json.load(fh).get("claudeAiOauth") or {}).get("expiresAt")
                if isinstance(_exp, (int, float)) and _exp / 1000.0 <= time.time():
                    continue
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if True:
                log.warning(
                    "router fail-open: %s — no usage data for any account, "
                    "routing to first candidate with a readable token",
                    name,
                )
                return AccountChoice(name=name, home_path=home_path)

    raise NoAccountsAvailable(
        f"no usable accounts (every candidate either ≥ "
        f"{saturation_cutoff:.0f}% peak utilization or unreachable)"
    )


# ---------------------------------------------------------------------------
# Resolve a stored session's account back to a HOME path.
# ---------------------------------------------------------------------------

def home_for_account(name: str | None) -> Path:
    """Resolve a stored ``account`` name (from session JSON) to the HOME
    path the claude subprocess should use.

    None / "main" / unknown / unreadable → ``MAIN_HOME`` (the bridge user's
    primary). This is the safe fallback: a session locked to an account
    that's been removed since creation continues to work as if it were
    on main, instead of crashing.
    """
    if not name or name == "main":
        return MAIN_HOME
    candidate = WIZERITH_ACCOUNTS_ROOT / name
    if (candidate / ".claude" / ".credentials.json").is_file():
        return candidate
    log.warning("session referenced account %s; falling back to main", name)
    return MAIN_HOME


# ---------------------------------------------------------------------------
# Test seam
# ---------------------------------------------------------------------------

def _reset_for_tests() -> None:
    with _lock:
        _usage_cache.clear()
        _hot_until.clear()


__all__ = [
    "AccountChoice",
    "MAIN_HOME",
    "NoAccountsAvailable",
    "WIZERITH_ACCOUNTS_ROOT",
    "home_for_account",
    "is_usable",
    "list_accounts",
    "mark_hot",
    "note_success",
    "pick",
    "snapshot_usage",
]
