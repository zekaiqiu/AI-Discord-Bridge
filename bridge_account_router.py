"""Bridge-side multi-account routing.

Counterpart of ``Multi-Agent-Framework/src/pipeline/account_pool.py`` and
``portfolio-tool/services/chat/account_router.py``, scoped to what the
Discord bridge needs:

  * A single global active-account choice (the bridge's claude conversation
    is global — `--continue` resumes the most recent claude session in
    HOME, not per-channel — so we don't fan account state out per channel).
  * Persisted to disk so we survive bridge restarts on the same account
    (avoids gratuitous fresh-session triggers when the service flaps).
  * Auto-switch when the currently-recorded account crosses the
    saturation cutoff. Switching forces the next subprocess to drop
    ``--continue`` because claude's session history is keyed by HOME,
    so a switched HOME has no prior session.
  * Sticky-during-conversation: don't honor an auto-switch while the
    user is actively sending messages (``STICKY_IDLE_SECONDS`` since
    the last subprocess). Losing ``--continue`` mid-debug is worse
    than briefly running into a real 429; we'd rather surface that and
    let the user retry than silently amnesia them mid-thread.

We don't share the chat repo's ``account_router.py`` directly because the
bridge is a separate Python process tree under
``~/projects/claude-bridge`` with its own venv — pulling in the chat
container's module would couple two unrelated deploy lifecycles. The
duplicated logic is ~200 lines and identical in shape; if a third copy
ships we extract a real shared library.

Active marker
=============
Both ``record_active()`` and the pipeline's ``account_pool.record_active``
write to ``~/.cache/wizerith-active-account``. ``!usage``'s ``← active``
display reads that file. So whichever service ran most recently wins the
display arrow — bridge writes on each subprocess launch, pipeline writes
on each invoke pick.

The bridge's OWN state — what account our next ``--continue`` is bound to
— lives separately in ``~/.cache/wizerith-bridge-active-account`` so the
display marker can be overwritten by the pipeline without us silently
re-routing mid-conversation to an account whose claude session history
has nothing in it.
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

log = logging.getLogger("bridge.account_router")


# ---------------------------------------------------------------------------
# Paths + tuning
# ---------------------------------------------------------------------------

WIZERITH_ACCOUNTS_ROOT = Path(
    os.environ.get("WIZERITH_ACCOUNTS_ROOT", "/opt/wizerith/claude-accounts")
)

MAIN_HOME = Path(os.environ.get("BRIDGE_MAIN_HOME", str(Path.home())))
MAIN_CREDENTIALS = MAIN_HOME / ".claude" / ".credentials.json"

# Where the bridge persists its own current-account choice across restarts.
# Distinct from the display marker so the pipeline's writes can't clobber
# our session-bound HOME (see module docstring).
BRIDGE_ACTIVE_FILE = Path(
    os.environ.get(
        "BRIDGE_ACTIVE_ACCOUNT_FILE",
        str(Path.home() / ".cache" / "wizerith-bridge-active-account"),
    )
)

# Timestamp (unix seconds, written as string) of the most recent claude
# subprocess we routed. Used to suppress auto-switch while a conversation
# is actively flowing — switching loses --continue history, which is fine
# at conversation boundaries but ruins context mid-debug. If the current
# message arrives within STICKY_IDLE_SECONDS of the prior one, we keep
# the bound account even if it's over the saturation cutoff and accept
# that a real 429 may surface; the next message after the user goes idle
# will re-evaluate and rotate normally.
BRIDGE_LAST_RUN_FILE = Path(
    os.environ.get(
        "BRIDGE_LAST_RUN_FILE",
        str(Path.home() / ".cache" / "wizerith-bridge-last-run"),
    )
)

STICKY_IDLE_SECONDS = float(os.environ.get("BRIDGE_STICKY_IDLE_SECONDS", "900"))

# Hard saturation ceiling. The sticky-during-conversation guard defers an
# auto-switch to preserve --continue history, but ONLY within the soft band
# [saturation cutoff, HARD_SWITCH_PCT). At or above this ceiling a 429 is
# imminent, so continuing on the account would just fail the turn — we switch
# regardless of how recently the user messaged. This is the backstop that
# stops a single hot account from getting pinned at 100% through a long,
# unbroken conversation (e.g. a multi-turn build) and racking up overage.
HARD_SWITCH_PCT = float(os.environ.get("BRIDGE_HARD_SWITCH_PCT", "95"))

# ---------------------------------------------------------------------------
# Emergency non-Anthropic provider (qwen3.5 via haihub, OpenAI-compatible).
# ---------------------------------------------------------------------------
# When every *reachable* Anthropic account is at/above the saturation cutoff,
# the router can route the turn to qwen3.5 instead of the old "limp on the
# bound account / fall back to main and let the 429 surface" behavior. This is
# a last resort: it only triggers on the same condition that makes pick() raise
# NoAccountsAvailable. It is gated on a resolvable HAIHUB_API_KEY — if the key
# is not configured we keep the previous Anthropic-only behavior so we never
# route to a provider we can't authenticate to.
QWEN_EMERGENCY_NAME = "qwen-emergency"

# The bridge process does not carry HAIHUB_API_KEY in its own env — the key
# lives in portfolio-tool's .env (consumed by the chat container). Resolve from
# the process env first, then fall back to reading that file so "use the
# existing key" works without duplicating the secret into a second location.
HAIHUB_ENV_FALLBACK_FILE = Path(
    os.environ.get("HAIHUB_ENV_FILE", "/home/felix/projects/portfolio-tool/.env")
)


def resolve_haihub_key() -> str | None:
    """Return the haihub API key from the env, else from the fallback .env file.

    Returns None when neither source yields a non-empty value — callers treat
    that as "emergency provider unavailable" and keep the Anthropic-only path.
    """
    key = os.environ.get("HAIHUB_API_KEY")
    if key:
        return key
    try:
        for raw in HAIHUB_ENV_FALLBACK_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("HAIHUB_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                return val or None
    except OSError:
        return None
    return None


def qwen_emergency_available() -> bool:
    return resolve_haihub_key() is not None

# Display marker shared with pipeline + chat. !usage reads this.
DISPLAY_ACTIVE_FILE = Path(
    os.environ.get(
        "WIZERITH_ACTIVE_ACCOUNT_FILE",
        str(Path.home() / ".cache" / "wizerith-active-account"),
    )
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_BETA = "oauth-2025-04-20"
USAGE_FETCH_TIMEOUT_SEC = 10.0
USAGE_CACHE_TTL_SEC = 60.0

# The fast rolling window — graceful-retire at the headroom cutoff (default
# 80%) so a few in-flight calls land before it actually trips.
_FIVE_HOUR_KEY = "five_hour"

# The slow weekly windows. These retire only at a *full* 100%: a 7-day window
# is a week-long budget, so excluding an account the moment it crosses 80%
# strands up to a fifth of the weekly plan unused and refuses to route to the
# account for days even though every call would still be free plan quota.
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


class NoAccountsAvailable(RuntimeError):
    """Raised when ``pick()`` finds zero candidates or all are saturated.
    Caller falls back to ``main`` (felix's primary) and lets the rate-
    limit surface naturally if main is also tight."""


@dataclass(frozen=True)
class AccountChoice:
    name: str
    home_path: Path


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def list_accounts() -> list[tuple[str, Path]]:
    accounts: list[tuple[str, Path]] = []
    if MAIN_CREDENTIALS.is_file():
        accounts.append(("main", MAIN_HOME))
    if WIZERITH_ACCOUNTS_ROOT.is_dir():
        try:
            for entry in sorted(WIZERITH_ACCOUNTS_ROOT.iterdir()):
                if not entry.is_dir() or not entry.name:
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
_usage_cache: dict[str, tuple[dict[str, Any], float]] = {}
# Per-account last probe status (USAGE_OK / USAGE_AUTH_DEAD / USAGE_UNREACHABLE),
# stamped on each snapshot_usage(). Drives auth-failover: a token rejected with a
# 401/403 means the account can serve NO request and must be switched away from
# (even mid-conversation), whereas a network/timeout failure is transient and the
# bound account is kept.
_auth_status: dict[str, tuple[str, float]] = {}


def _read_token(creds_path: Path) -> str | None:
    try:
        with open(creds_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        oauth = data.get("claudeAiOauth") or {}
        return oauth.get("accessToken") or None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("reading %s failed: %s", creds_path, exc)
        return None


# Probe outcome classes. A definitively-rejected token (the account's OAuth creds
# are expired/invalid → it can serve NO request) is distinct from a transient
# network/timeout failure (the token may be fine → don't churn the bound account).
USAGE_OK = "ok"
USAGE_AUTH_DEAD = "auth_dead"
USAGE_UNREACHABLE = "unreachable"


def _fetch_usage_status(access_token: str) -> tuple[dict[str, Any] | None, str]:
    if not access_token:
        return None, USAGE_AUTH_DEAD
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": ANTHROPIC_BETA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=USAGE_FETCH_TIMEOUT_SEC) as resp:
            return json.loads(resp.read()), USAGE_OK
    except urllib.error.HTTPError as exc:
        # 401/403 = the bearer was rejected → this account's auth is dead (token
        # expired / refresh failed → needs re-login). Any other status (5xx, 429,
        # …) is a service hiccup against a token that was still accepted.
        # NB: HTTPError subclasses URLError, so this except MUST precede it.
        if exc.code in (401, 403):
            log.warning("usage fetch: token rejected (HTTP %s) — account auth dead", exc.code)
            return None, USAGE_AUTH_DEAD
        log.warning("usage fetch failed (HTTP %s)", exc.code)
        return None, USAGE_UNREACHABLE
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("usage fetch failed: %s", exc)
        return None, USAGE_UNREACHABLE


def _fetch_usage(access_token: str) -> dict[str, Any] | None:
    snap, _status = _fetch_usage_status(access_token)
    return snap


def account_auth_dead(name: str) -> bool:
    """True when this account's most recent probe was a definitive auth rejection
    (HTTP 401/403, or no readable token at all). Network-unreachable does NOT
    count. Reads the status stamped by the last ``snapshot_usage(name, ...)`` —
    callers probe first, then consult this."""
    with _lock:
        st = _auth_status.get(name)
    return bool(st and st[0] == USAGE_AUTH_DEAD)


def snapshot_usage(name: str, home_path: Path, *, force_refresh: bool = False) -> dict[str, Any] | None:
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
        # No readable bearer at all → the account can't authenticate.
        with _lock:
            _auth_status[name] = (USAGE_AUTH_DEAD, now)
        return None
    snap, status = _fetch_usage_status(token)
    with _lock:
        _auth_status[name] = (status, now)
    if snap is None:
        return None
    with _lock:
        _usage_cache[name] = (snap, now)
    return snap


def peak_utilization(snapshot: dict[str, Any] | None) -> float:
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


def is_plan_saturated(
    snapshot: dict[str, Any] | None, saturation_cutoff: float
) -> bool:
    """True when the account has no usable plan headroom left.

    Per-window thresholds — the two window classes age very differently:

      * ``five_hour`` retires at ``saturation_cutoff`` (default 80%). The
        20% margin lets a few in-flight calls land before the fast window
        actually trips into a 429.
      * the weekly (``seven_day*``) windows retire only at a full 100%.
        They refill once a week, so retiring them at 80% would strand up to
        a fifth of the weekly plan and lock the account out for *days*,
        even though every call in the 80–99% band is still free plan quota.

    Overage / ``extra_usage`` is deliberately NOT consulted here: paid
    overage only bills once the plan windows are exhausted, so while any
    plan window has headroom the extra-usage cap is irrelevant to whether
    the account can serve a request for free.
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


def is_account_in_overage(snapshot: dict[str, Any] | None) -> bool:
    """True when the account has already spent into paid overage.

    Anthropic returns the overage meter as ``extra_usage`` in the raw
    /oauth/usage snapshot: ``is_enabled`` plus ``used_credits`` /
    ``monthly_limit`` in CENTS (minor units, despite ``currency: USD``).
    An account is "in overage" once ``used_credits > 0`` — at some point this
    month it exhausted its plan allotment and spilled into paid credits.

    INFORMATIONAL ONLY — this is no longer a routing exclusion. The extra-
    usage cap must not matter while plan quota has not been exceeded: an
    account whose 5h/7d window has free headroom serves the next call from
    plan at $0 regardless of how much overage it racked up earlier in the
    month, so excluding it just strands free plan quota. Routing therefore
    gates purely on :func:`is_plan_saturated`; once an account *is* plan-
    saturated it is excluded on that basis, at which point any further spend
    would be overage anyway. Retained for ``!usage`` display and logging.
    """
    if not isinstance(snapshot, dict):
        return False
    extra = snapshot.get("extra_usage")
    if not isinstance(extra, dict) or not extra.get("is_enabled"):
        return False
    used = extra.get("used_credits") or 0.0
    try:
        return float(used) > 0.0
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Pick
# ---------------------------------------------------------------------------

def pick(*, min_headroom_pct: float = 20.0) -> AccountChoice:
    """Return the account with the most plan headroom.

    An account is excluded only when it is plan-saturated
    (:func:`is_plan_saturated`): its 5-hour window is ≥ the saturation cutoff
    (default ≥80%, leaving the 20% 429-safety margin) OR any 7-day window is
    fully consumed at ≥100%. Paid overage is NOT an exclusion — while an
    account still has plan headroom the next call is free regardless of past
    overage spend, so the extra-usage cap is irrelevant to routing here.

    Among the survivors, lowest plan peak wins (spread load so accounts drain
    together). Raises ``NoAccountsAvailable`` when every account is plan-
    saturated or unreadable. The caller then falls back to the qwen emergency
    provider (free) if a key is configured, else limps on the bound account
    and lets any rate-limit surface.
    """
    saturation_cutoff = 100.0 - min_headroom_pct
    candidates = list_accounts()
    if not candidates:
        raise NoAccountsAvailable("no accounts on disk")

    scored: list[tuple[str, Path, float]] = []
    for name, home_path in candidates:
        snap = snapshot_usage(name, home_path)
        if snap is None:
            log.info("router skip %s: usage snapshot unavailable", name)
            continue
        if is_plan_saturated(snap, saturation_cutoff):
            log.info(
                "router skip %s: plan-saturated (peak %.1f%%, 5h cutoff "
                "%.1f%% / 7d cutoff 100%%)",
                name, peak_utilization(snap), saturation_cutoff,
            )
            continue
        scored.append((name, home_path, peak_utilization(snap)))

    if not scored:
        raise NoAccountsAvailable(
            f"no account with free plan quota: every account is plan-saturated "
            f"(5h ≥ {saturation_cutoff:.0f}% or 7d ≥ 100%) or unreadable"
        )

    scored.sort(key=lambda t: (t[2], t[0]))
    name, home_path, _ = scored[0]
    return AccountChoice(name=name, home_path=home_path)


# ---------------------------------------------------------------------------
# Resolve a stored account name to a HOME path.
# ---------------------------------------------------------------------------

def home_for_account(name: str | None) -> Path:
    if not name or name == "main":
        return MAIN_HOME
    candidate = WIZERITH_ACCOUNTS_ROOT / name
    if (candidate / ".claude" / ".credentials.json").is_file():
        return candidate
    log.warning("bridge referenced account %s; falling back to main", name)
    return MAIN_HOME


# ---------------------------------------------------------------------------
# Persistent state — what account the bridge is currently bound to.
# ---------------------------------------------------------------------------

def read_current_account() -> str | None:
    """Return the bridge's current account name, or None if nothing
    persisted yet (first-run case → caller picks)."""
    try:
        text = BRIDGE_ACTIVE_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("read %s failed: %s", BRIDGE_ACTIVE_FILE, exc)
        return None
    return text or None


def write_current_account(name: str) -> None:
    try:
        BRIDGE_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        BRIDGE_ACTIVE_FILE.write_text(name, encoding="utf-8")
    except OSError as exc:
        log.warning("write %s failed: %s", BRIDGE_ACTIVE_FILE, exc)


def record_active(name: str) -> None:
    """Update the shared !usage display marker to ``name``. Best-effort —
    a write failure here is purely cosmetic for the !usage report."""
    try:
        DISPLAY_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        DISPLAY_ACTIVE_FILE.write_text(name, encoding="utf-8")
    except OSError as exc:
        log.warning("write %s failed: %s", DISPLAY_ACTIVE_FILE, exc)


def read_last_run() -> float | None:
    """Unix-timestamp of the most recent route decision, or None if the
    bridge hasn't run yet (or the file got nuked)."""
    try:
        text = BRIDGE_LAST_RUN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("read %s failed: %s", BRIDGE_LAST_RUN_FILE, exc)
        return None
    try:
        return float(text)
    except ValueError:
        return None


def record_last_run(ts: float | None = None) -> None:
    """Stamp the last-run timestamp. Best-effort — if write fails the only
    impact is the next message may switch when it would have stuck."""
    if ts is None:
        ts = time.time()
    try:
        BRIDGE_LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
        BRIDGE_LAST_RUN_FILE.write_text(f"{ts:.3f}", encoding="utf-8")
    except OSError as exc:
        log.warning("write %s failed: %s", BRIDGE_LAST_RUN_FILE, exc)


# ---------------------------------------------------------------------------
# Entry point used by bot.run_claude.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteDecision:
    """Result of resolve_account_for_run.

    * ``name``/``home_path`` — what to set HOME to on the subprocess.
    * ``switched`` — True when the bridge moved off its previously-bound
      account (caller must drop ``--continue`` because the new HOME has
      no claude session history yet).
    * ``previous`` — the prior bound name, for surfaced diagnostics
      ("switching from main to account-2"). None when first-run.
    """
    name: str
    home_path: Path
    switched: bool
    previous: str | None
    # "anthropic" for a normal OAuth-account route (run the claude CLI against
    # home_path); "qwen" for the emergency provider (home_path is a placeholder
    # and unused — the caller dispatches to the haihub OpenAI-compatible path).
    provider: str = "anthropic"


def _qwen_emergency_decision(previous: str | None) -> RouteDecision:
    """Build + persist a route to the emergency qwen provider.

    Persisted under QWEN_EMERGENCY_NAME so the next turn knows it's currently on
    the fallback and should try to climb back to a healthy Anthropic account."""
    write_current_account(QWEN_EMERGENCY_NAME)
    record_active(QWEN_EMERGENCY_NAME)
    record_last_run()
    return RouteDecision(
        name=QWEN_EMERGENCY_NAME,
        home_path=MAIN_HOME,  # placeholder; the qwen path doesn't set HOME
        switched=previous != QWEN_EMERGENCY_NAME,
        previous=previous,
        provider="qwen",
    )


def resolve_account_for_run(
    *,
    force_pick: bool = False,
    ignore_sticky: bool = False,
    min_headroom_pct: float = 20.0,
) -> RouteDecision:
    """Decide what account the next ``claude`` subprocess should use.

    ``force_pick=True`` ignores the persisted current account and re-picks
    fresh — used when the caller already wants a fresh session (e.g.
    ``!newsession``) so we route to the freshest account at the moment
    of conversation start.

    ``ignore_sticky=True`` disables the sticky-during-conversation guard so a
    saturated bound account is abandoned immediately. The guard exists ONLY to
    protect ``--continue`` history; callers starting a fresh session (no
    ``--continue`` to lose) pass this so they never inherit a pinned, saturated
    account. Unlike ``force_pick`` it does NOT re-pick when the bound account
    is healthy — so it can't flap between near-tied accounts.

    Otherwise: keep the persisted account if it's still under the
    saturation cutoff. If it's saturated (or nonexistent / unreadable),
    re-pick — which counts as a switch from the caller's perspective and
    forces ``--continue`` off in the subprocess. Even within the sticky
    window, an account at or above ``HARD_SWITCH_PCT`` is abandoned anyway:
    losing context mid-conversation beats failing the turn on a 429.
    """
    previous = read_current_account()

    if force_pick:
        try:
            choice = pick(min_headroom_pct=min_headroom_pct)
        except NoAccountsAvailable:
            if qwen_emergency_available():
                log.warning("bridge: all accounts saturated on force-pick; "
                            "routing to qwen emergency provider")
                return _qwen_emergency_decision(previous)
            choice = AccountChoice(name="main", home_path=MAIN_HOME)
        switched = previous is not None and previous != choice.name
        write_current_account(choice.name)
        record_active(choice.name)
        record_last_run()
        return RouteDecision(
            name=choice.name, home_path=choice.home_path,
            switched=switched, previous=previous,
        )

    if previous == QWEN_EMERGENCY_NAME:
        # Currently on the emergency provider. Try to recover to a healthy
        # Anthropic account on every turn — qwen is a fallback, not a home.
        # There is no --continue history to protect (qwen is stateless), so
        # sticky never applies here. Only stay on qwen while all Anthropic
        # accounts remain saturated.
        try:
            choice = pick(min_headroom_pct=min_headroom_pct)
        except NoAccountsAvailable:
            return _qwen_emergency_decision(previous)
        log.info("bridge: recovering off qwen emergency provider → %s", choice.name)
        write_current_account(choice.name)
        record_active(choice.name)
        record_last_run()
        return RouteDecision(
            name=choice.name, home_path=choice.home_path,
            switched=True, previous=previous,
        )

    if previous is not None:
        # Verify persisted choice is still healthy.
        home = home_for_account(previous)
        snap = snapshot_usage(previous, home)
        peak = peak_utilization(snap)
        cutoff = 100.0 - min_headroom_pct
        # AUTH FAILOVER: if the bound account's token was definitively rejected
        # (HTTP 401/403, or unreadable creds — expired / refresh-dead, needs
        # re-login), it can serve NO turn. Abandon it immediately regardless of
        # the sticky window — there is no --continue history worth protecting on
        # an account that 401s every call. A network/timeout failure does NOT
        # trip this (account_auth_dead only fires on a real auth rejection), so a
        # transient API hiccup still keeps the bound account as before.
        if snap is None and account_auth_dead(previous):
            log.warning(
                "bridge: bound account %s auth is dead (token rejected) — "
                "failing over to a working account", previous,
            )
            try:
                choice = pick(min_headroom_pct=min_headroom_pct)
            except NoAccountsAvailable:
                if qwen_emergency_available():
                    return _qwen_emergency_decision(previous)
                log.warning(
                    "bridge: no working account to fail over to from %s "
                    "(all dead/saturated, no qwen) — limping on it", previous,
                )
                choice = AccountChoice(name=previous, home_path=home)
            if choice.name != previous:
                write_current_account(choice.name)
                record_active(choice.name)
                record_last_run()
                return RouteDecision(
                    name=choice.name, home_path=choice.home_path,
                    switched=True, previous=previous,
                )
        # If we couldn't fetch usage we keep using the account anyway —
        # transient API hiccup shouldn't trigger a session reset. The
        # actual rate-limit surface (if real) will hit the next call's
        # response; that's the same behavior as before this routing
        # existed.
        #
        # Re-evaluate only on plan saturation (5h ≥ cutoff or 7d ≥ 100%).
        # Overage no longer unpins a bound account: while the bound account
        # still has plan headroom the next call is free regardless of past
        # overage, so churning --continue to chase a "cleaner" account would
        # only lose conversation context for no saving.
        if snap is not None and is_plan_saturated(snap, cutoff):
            # Sticky-during-conversation: switching loses --continue
            # history, so don't do it mid-burst. Only honor the switch
            # if the user has been idle long enough that no live thread
            # of context will get cut. The next message after idle will
            # re-evaluate and rotate normally. Trade-off: if usage stays
            # >cutoff and the user keeps typing, we eventually surface a
            # real 429 — that's preferable to context loss mid-debug.
            last_run = read_last_run()
            now = time.time()
            recent = last_run is not None and (now - last_run) < STICKY_IDLE_SECONDS
            # Sticky only protects the SOFT band [cutoff, HARD_SWITCH_PCT) on a
            # continuing session. ignore_sticky (fresh session — no --continue
            # to lose) or a peak at/above the hard ceiling overrides it.
            within_sticky = (
                recent
                and not ignore_sticky
                and peak < HARD_SWITCH_PCT
            )
            if within_sticky:
                log.info(
                    "bridge: %s peak %.1f%% ≥ %.1f%% but last run %.0fs ago "
                    "< %.0fs sticky window (soft band, < %.1f%% hard ceiling); "
                    "staying for session continuity",
                    previous, peak, cutoff, now - last_run, STICKY_IDLE_SECONDS,
                    HARD_SWITCH_PCT,
                )
                record_active(previous)
                record_last_run(now)
                return RouteDecision(
                    name=previous, home_path=home,
                    switched=False, previous=previous,
                )
            log.info(
                "bridge: %s needs re-eval (plan-saturated, peak %.1f%%, "
                "cutoff %.1f%%); auto-switching (ignore_sticky=%s, "
                "hard_ceiling=%s, idle %.0fs / %.0fs window)",
                previous, peak, cutoff, ignore_sticky,
                peak >= HARD_SWITCH_PCT,
                (now - last_run) if last_run is not None else -1.0,
                STICKY_IDLE_SECONDS,
            )
            try:
                choice = pick(min_headroom_pct=min_headroom_pct)
            except NoAccountsAvailable:
                if qwen_emergency_available():
                    # Every Anthropic account is saturated and we'd otherwise
                    # switch (idle past sticky, or at/above the hard ceiling) —
                    # route to the emergency provider instead of limping on a
                    # saturated account and 429ing.
                    log.warning("bridge: all accounts saturated; routing to "
                                "qwen emergency provider (from %s)", previous)
                    return _qwen_emergency_decision(previous)
                # No emergency provider — keep limping on the previously-bound
                # account; the rate-limit will surface naturally.
                log.warning("bridge: all accounts saturated; staying on %s", previous)
                record_active(previous)
                record_last_run(now)
                return RouteDecision(
                    name=previous, home_path=home,
                    switched=False, previous=previous,
                )
            if choice.name == previous:
                # Pick returned the same account — somehow not actually
                # over the cutoff after re-eval. Treat as no switch.
                record_active(choice.name)
                record_last_run(now)
                return RouteDecision(
                    name=choice.name, home_path=choice.home_path,
                    switched=False, previous=previous,
                )
            write_current_account(choice.name)
            record_active(choice.name)
            record_last_run(now)
            return RouteDecision(
                name=choice.name, home_path=choice.home_path,
                switched=True, previous=previous,
            )
        record_active(previous)
        record_last_run()
        return RouteDecision(
            name=previous, home_path=home,
            switched=False, previous=previous,
        )

    # First-run case — no persisted account.
    try:
        choice = pick(min_headroom_pct=min_headroom_pct)
    except NoAccountsAvailable:
        if qwen_emergency_available():
            log.warning("bridge: all accounts saturated on first run; "
                        "routing to qwen emergency provider")
            return _qwen_emergency_decision(previous)
        choice = AccountChoice(name="main", home_path=MAIN_HOME)
    write_current_account(choice.name)
    record_active(choice.name)
    record_last_run()
    return RouteDecision(
        name=choice.name, home_path=choice.home_path,
        switched=False, previous=None,
    )


# ---------------------------------------------------------------------------
# Test seam
# ---------------------------------------------------------------------------

def _reset_for_tests() -> None:
    with _lock:
        _usage_cache.clear()
        _auth_status.clear()


__all__ = [
    "AccountChoice",
    "BRIDGE_ACTIVE_FILE",
    "BRIDGE_LAST_RUN_FILE",
    "DISPLAY_ACTIVE_FILE",
    "MAIN_HOME",
    "NoAccountsAvailable",
    "QWEN_EMERGENCY_NAME",
    "RouteDecision",
    "STICKY_IDLE_SECONDS",
    "WIZERITH_ACCOUNTS_ROOT",
    "home_for_account",
    "is_account_in_overage",
    "is_plan_saturated",
    "qwen_emergency_available",
    "resolve_haihub_key",
    "list_accounts",
    "peak_utilization",
    "pick",
    "read_current_account",
    "read_last_run",
    "record_active",
    "record_last_run",
    "resolve_account_for_run",
    "snapshot_usage",
    "write_current_account",
]
