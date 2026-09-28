"""!usage handler: live Anthropic quota windows + cost from a maintained price table.

Two data sources, both read directly without an LLM:

1. ``GET /api/oauth/usage`` (Anthropic) — the live rate-limit windows
   (5-hour, 7-day, 7-day-Opus/Sonnet, etc.). These are the numbers the
   pipeline orchestrator watches; when any window crosses TRIPPED_PCT
   (80%) it auto-pauses projects with ``cause=usage_high``.

2. ``ccusage`` (npm: ``ccusage``) — local Claude Code transcripts parsed
   for raw per-model token counts (input / output / cache-write /
   cache-read). We do NOT use ccusage's ``cost`` field. Instead the cost
   for every line is computed from those raw token counts using the
   ``_PRICES`` table below, which mirrors Anthropic's published pricing
   page. This way the bridge has one place to keep prices current and
   the report always reflects the actual price per token.

Cache-write rate assumption: ccusage emits a single ``cacheCreationTokens``
field that does not split 5-minute vs 1-hour writes. Claude Code's default
is 5-minute caching, so we apply the 5-minute multiplier (1.25× input).
That matches the historical numbers ccusage emits as well.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_FRAMEWORK_SRC = Path("/home/felix/Multi-Agent-Framework/src")
if _FRAMEWORK_SRC.is_dir() and str(_FRAMEWORK_SRC) not in sys.path:
    sys.path.insert(0, str(_FRAMEWORK_SRC))

try:
    from pipeline.credentials import get_oauth as _get_oauth  # type: ignore
except Exception:  # pragma: no cover - framework not installed
    _get_oauth = None  # type: ignore


_CCUSAGE_TIMEOUT = 10
_API_TIMEOUT = 10
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_ANTHROPIC_BETA = "oauth-2025-04-20"

# Account discovery. The bridge already runs as the user that owns ~/.claude
# (the "main" account); additional accounts are onboarded under
# the extra-accounts root below by claude-as-init. !usage iterates all
# accounts it can find and reports each.
_MAIN_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
_EXTRA_ACCOUNTS_ROOT = Path("/opt/wizerith/claude-accounts")
# State file written by the pipeline orchestrator's account_pool integration
# (when that ships). Each line: ``<account_name>\n``. The most-recent line is
# the currently-active account. Absent file → main is active.
_ACTIVE_ACCOUNT_FILE = Path.home() / ".cache" / "wizerith-active-account"

_WINDOW_LABELS: list[tuple[str, str]] = [
    ("five_hour", "5h"),
    ("seven_day", "7d"),
    ("seven_day_opus", "7d Opus"),
    ("seven_day_sonnet", "7d Sonnet"),
    ("seven_day_cowork", "7d cowork"),
    ("seven_day_omelette", "7d omelette"),
    ("seven_day_oauth_apps", "7d oauth apps"),
]


# ---------------------------------------------------------------------------
# Price table — USD per million tokens.
# Source: https://platform.claude.com/docs/en/docs/about-claude/pricing
# (Mirrors the "Model pricing" table; check on each Anthropic price update.)
# Cache-write: 1.25× input (5-minute) is what we apply since Claude Code
#              defaults to 5m caches; 1h would be 2.0×.
# Cache-read:  0.10× input.
# ---------------------------------------------------------------------------

class Price:
    """Per-million-token prices in USD for one model."""
    __slots__ = ("input", "output")

    def __init__(self, inp: float, out: float) -> None:
        self.input = inp
        self.output = out

    @property
    def cache_write_5m(self) -> float:
        return self.input * 1.25

    @property
    def cache_write_1h(self) -> float:
        return self.input * 2.0

    @property
    def cache_read(self) -> float:
        return self.input * 0.10


# Keys are normalised model family identifiers — e.g. "opus-4-7", "haiku-4-5".
# Anthropic's ccusage emits longer strings ("claude-opus-4-7",
# "claude-haiku-4-5-20251001") that ``_normalise_model`` collapses to one of
# these keys.
_PRICES: dict[str, Price] = {
    # Opus 4.5+ are on the cheaper Opus tier.
    "opus-4-8": Price(5.0, 25.0),
    "opus-4-7": Price(5.0, 25.0),
    "opus-4-6": Price(5.0, 25.0),
    "opus-4-5": Price(5.0, 25.0),
    # Opus 4.0 / 4.1 use the legacy expensive tier.
    "opus-4-1": Price(15.0, 75.0),
    "opus-4-0": Price(15.0, 75.0),
    "opus-4":   Price(15.0, 75.0),
    "opus-3":   Price(15.0, 75.0),
    # Sonnet 4.x all share one tier.
    "sonnet-4-6": Price(3.0, 15.0),
    "sonnet-4-5": Price(3.0, 15.0),
    "sonnet-4-0": Price(3.0, 15.0),
    "sonnet-4":   Price(3.0, 15.0),
    "sonnet-3-7": Price(3.0, 15.0),
    # Haiku.
    "haiku-4-5": Price(1.0, 5.0),
    "haiku-4":   Price(1.0, 5.0),
    "haiku-3-5": Price(0.80, 4.0),
    "haiku-3":   Price(0.25, 1.25),
}

# Fallback when we see a brand-new model. Conservative: assume Sonnet rates,
# the median tier, and tag the family as "unknown" in the output so the user
# can spot it and add a row to _PRICES.
_FALLBACK_PRICE = Price(3.0, 15.0)


def _normalise_model(raw: str) -> str:
    """Collapse a ccusage model string to the short family key used in _PRICES.

    Examples:
      claude-opus-4-7              -> opus-4-7
      claude-haiku-4-5-20251001    -> haiku-4-5  (date suffix dropped)
      claude-sonnet-4-5            -> sonnet-4-5
      claude-3-7-sonnet-20250219   -> sonnet-3-7 (legacy ordering tolerated)
    """
    name = raw.removeprefix("claude-").lower()
    parts = [p for p in name.split("-") if p]
    if not parts:
        return raw
    # Strip a trailing all-digits chunk longer than 3 chars (release date).
    if parts[-1].isdigit() and len(parts[-1]) > 3:
        parts = parts[:-1]
    families = {"opus", "sonnet", "haiku"}
    fam_idx = next((i for i, p in enumerate(parts) if p in families), None)
    if fam_idx is None:
        return "-".join(parts)
    family = parts[fam_idx]
    # Take the (up to) two version chunks adjacent to the family name. Both
    # "opus-4-7" and "3-7-sonnet" shapes show up in the wild — pick whichever
    # side has version digits.
    nums_after = [p for p in parts[fam_idx + 1 : fam_idx + 3] if p.isdigit()]
    nums_before = [p for p in parts[:fam_idx] if p.isdigit()]
    nums = nums_after if nums_after else nums_before
    if not nums:
        return family
    return f"{family}-" + "-".join(nums[:2])


def _price_for(model_raw: str) -> tuple[Price, str, bool]:
    """Resolve the price + short label for a model string. Returns (price,
    short, is_known) — ``is_known`` is False when we fell back to the default
    (so the report can flag it for follow-up).
    """
    short = _normalise_model(model_raw)
    price = _PRICES.get(short)
    if price is not None:
        return price, short, True
    # Try one fallback: drop the trailing version chunk and look up the
    # family-only entry (e.g. "opus-5-2" -> "opus-5" -> miss -> last try
    # "opus-4-7" pricing). For any genuinely new family we degrade to
    # _FALLBACK_PRICE.
    head = short.rsplit("-", 1)[0]
    price = _PRICES.get(head)
    if price is not None:
        return price, short, False
    return _FALLBACK_PRICE, short, False


def _cost_for_breakdown(b: dict[str, Any]) -> tuple[float, dict[str, float], str, bool]:
    """Compute cost in USD for a per-model breakdown dict from ccusage daily.

    Returns (total_cost, per_token_type_cost, short_name, is_known_pricing).
    """
    price, short, known = _price_for(b.get("modelName", ""))
    in_tok = b.get("inputTokens") or 0
    out_tok = b.get("outputTokens") or 0
    cw_tok = b.get("cacheCreationTokens") or 0
    cr_tok = b.get("cacheReadTokens") or 0
    parts = {
        "in":  in_tok / 1_000_000 * price.input,
        "out": out_tok / 1_000_000 * price.output,
        "cw":  cw_tok / 1_000_000 * price.cache_write_5m,
        "cr":  cr_tok / 1_000_000 * price.cache_read,
    }
    return sum(parts.values()), parts, short, known


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _run_ccusage(*args: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["ccusage", *args, "--offline", "--json"],
        capture_output=True, text=True, timeout=_CCUSAGE_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ccusage {' '.join(args)} exited {proc.returncode}: "
            f"{proc.stderr.strip()[:200]}"
        )
    return json.loads(proc.stdout)


def _discover_accounts() -> list[tuple[str, Path]]:
    """Return [(name, credentials_path), ...] for every account this user
    can see. ``main`` is the bridge user's primary account at ~/.claude;
    other accounts come from the extra-accounts root.

    Discovery is permissive: we list directory NAMES even when the contents
    are unreadable, so the report can flag "logged in but unreadable" cases
    (e.g. credentials owned by root with mode 700, the state immediately
    after sudo claude-as-init before chown). The actual readability check
    happens in ``_load_oauth_for``.
    """
    accounts: list[tuple[str, Path]] = []
    if _MAIN_CREDENTIALS.exists():
        accounts.append(("main", _MAIN_CREDENTIALS))
    if _EXTRA_ACCOUNTS_ROOT.is_dir():
        try:
            for entry in sorted(_EXTRA_ACCOUNTS_ROOT.iterdir()):
                # Skip stray empty-name dirs from typo'd onboarding.
                if not entry.is_dir() or not entry.name:
                    continue
                # Skip dotfile dirs (.claude, .npm, .local, …) — claude's
                # own startup occasionally drops those at the accounts-root
                # layer (e.g. when something invokes claude with HOME=the
                # root). They're never real accounts, and surfacing them as
                # "credentials unreadable" rows is just noise. Same for
                # `_`-prefixed dirs: reserved for sibling sub-pools (e.g.
                # _wizerith-ai-pool, a directory of symlinks the chat stack
                # uses as its accounts root) — not billable accounts.
                if entry.name.startswith((".", "_")):
                    continue
                accounts.append((entry.name, entry / ".claude" / ".credentials.json"))
        except PermissionError:
            pass
    return accounts


def _load_oauth_for(creds_path: Path) -> dict[str, Any] | None:
    """Read an account's credentials JSON. Returns the parsed claudeAiOauth
    dict, or None if unreadable / missing / malformed."""
    try:
        with open(creds_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("claudeAiOauth")
    except (OSError, json.JSONDecodeError):
        return None


def _fetch_quota_status(
    access_token: str, *, expires_at_ms: int | None = None
) -> tuple[str, dict[str, Any] | None]:
    """Hit /api/oauth/usage and classify the outcome so the render layer can
    print a useful reason instead of a blanket "no data".

    Returns (status, data) where status ∈ {
        "ok"              - data is the parsed JSON,
        "no_token"        - empty access_token,
        "expired"         - client-side: token's expiresAt is in the past,
        "auth_invalid"    - server returned 401/403 (refresh token also dead),
        "rate_limited"    - server 429 (Anthropic throttling) — try later,
        "error"           - any other failure (network, parse, 5xx).
    }
    """
    if not access_token:
        return ("no_token", None)
    # Client-side staleness check — avoids burning an obvious dead call AND
    # gives a clean "expired" verdict for the UI. Anthropic returns 429 for
    # expired-token attempts (not 401), so this is the only reliable signal
    # for "needs re-login" without contacting the server.
    if expires_at_ms is not None:
        import time
        if expires_at_ms < int(time.time() * 1000):
            return ("expired", None)
    try:
        req = urllib.request.Request(
            _USAGE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "anthropic-beta": _ANTHROPIC_BETA,
            },
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            return ("ok", json.loads(resp.read()))
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        if exc.code in (401, 403):
            return ("auth_invalid", None)
        if exc.code == 429:
            return ("rate_limited", None)
        return ("error", None)
    except Exception:
        return ("error", None)


def _fetch_quota_with_token(access_token: str) -> dict[str, Any] | None:
    """Hit Anthropic's /api/oauth/usage with a specific access token.
    Returns the parsed JSON or None on any error.

    Back-compat wrapper around `_fetch_quota_status`; callers that want
    the failure reason should use that directly."""
    status, data = _fetch_quota_status(access_token)
    return data if status == "ok" else None


def _fetch_anthropic_quota() -> dict[str, Any] | None:
    """Backwards-compat: quota for the bridge user's primary (main) account."""
    if _get_oauth is None:
        return None
    try:
        oauth = _get_oauth()
        return _fetch_quota_with_token(oauth.get("accessToken") or "")
    except Exception:
        return None


def _read_active_account() -> str:
    """Which account is the orchestrator currently using? Read from the
    pool's state file if present; default to ``main``. Empty/absent file →
    no pool active, so the bridge default (~/.claude) wins."""
    try:
        text = _ACTIVE_ACCOUNT_FILE.read_text(encoding="utf-8").strip()
        # Pool writes one line per pick; most-recent is the last line.
        last = text.splitlines()[-1].strip() if text else ""
        return last or "main"
    except (OSError, IndexError):
        return "main"


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _fmt_money(usd: float) -> str:
    return f"${usd:,.2f}"


def _fmt_tokens(n: int | float) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:.0f}"


def _fmt_duration(minutes: float) -> str:
    m = max(0, int(minutes))
    h, m = divmod(m, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _fmt_until(resets_at_iso: str, now: datetime) -> str:
    try:
        when = _parse_iso(resets_at_iso)
    except (ValueError, TypeError):
        return "?"
    return _fmt_duration((when - now).total_seconds() / 60)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _build_account_quota_lines(
    name: str,
    quota: dict[str, Any] | None,
    now: datetime,
    *,
    is_active: bool,
) -> list[str]:
    """Render a single account's quota block. Empty list if no quota data."""
    if not quota:
        return []
    marker = " ← active" if is_active else ""
    header = f"**{name}**{marker}"
    lines: list[str] = [header]
    shown = 0
    for key, label in _WINDOW_LABELS:
        win = quota.get(key)
        if not isinstance(win, dict):
            continue
        util = win.get("utilization")
        if util is None:
            continue
        if key not in ("five_hour", "seven_day") and util < 50:
            continue
        resets = win.get("resets_at")
        until = _fmt_until(resets, now) if resets else "?"
        filled = min(10, int(round(util / 10)))
        bar = "█" * filled + "░" * (10 - filled)
        lines.append(f"  {label:9} {bar} {util:>5.1f}%  (resets in {until})")
        shown += 1
    extra = quota.get("extra_usage") or {}
    if isinstance(extra, dict) and extra.get("is_enabled"):
        # used_credits / monthly_limit are returned in CENTS (minor units),
        # despite currency="USD". Divide by 100 to display real dollars —
        # historically we printed cents-as-dollars and a $8.49 charge read as
        # "$849", triggering a false quota panic.
        used = (extra.get("used_credits") or 0.0) / 100.0
        cap_cents = extra.get("monthly_limit")
        currency = extra.get("currency") or "USD"
        if cap_cents:
            lines.append(
                f"  extra-usage  {currency} {used:,.2f} / "
                f"{cap_cents / 100.0:,.2f} monthly cap"
            )
        else:
            # No cap set → overage is unbounded. Flag it: this is exactly the
            # condition that let one account run up charges with no ceiling.
            lines.append(
                f"  extra-usage  {currency} {used:,.2f} (⚠ NO monthly cap set)"
            )
    return lines if shown else []


def _build_quota_section(_unused: dict[str, Any] | None, now: datetime) -> list[str]:
    """Top-level "Plan quota" section, multi-account. Each discovered
    account renders its own subsection; the active one is marked.
    Unreadable accounts (e.g. just-onboarded but not chown'd) get a stub
    so the operator notices and can fix permissions."""
    accounts = _discover_accounts()
    if not accounts:
        return []
    active = _read_active_account()
    lines: list[str] = ["**Plan quota — accounts** (resets in parens):"]
    rendered = 0
    for name, creds_path in accounts:
        oauth = _load_oauth_for(creds_path)
        if oauth is None:
            lines.append(f"**{name}** — credentials unreadable ({creds_path})")
            lines.append(
                f"  (run: sudo chown -R felix:felix {_EXTRA_ACCOUNTS_ROOT}/)"
            )
            rendered += 1
            continue
        token = oauth.get("accessToken") or ""
        expires_at = oauth.get("expiresAt")
        status, quota = _fetch_quota_status(
            token,
            expires_at_ms=int(expires_at) if isinstance(expires_at, (int, float)) else None,
        )
        if status != "ok":
            # Classify the failure so the operator knows what to do, instead
            # of a blanket "no data" that hides expired-creds vs throttling.
            if status == "expired":
                lines.append(
                    f"**{name}** — OAuth access token expired "
                    f"(re-login: `HOME=<account-home> claude /login`)"
                )
            elif status == "auth_invalid":
                lines.append(
                    f"**{name}** — credentials invalid (refresh token dead — "
                    f"re-login: `HOME=<account-home> claude /login`)"
                )
            elif status == "rate_limited":
                lines.append(f"**{name}** — Anthropic rate-limited the usage query (try again shortly)")
            elif status == "no_token":
                lines.append(f"**{name}** — no access token in credentials file")
            else:
                lines.append(f"**{name}** — usage API returned no data")
            rendered += 1
            continue
        block = _build_account_quota_lines(
            name, quota, now, is_active=(name == active)
        )
        if not block:
            lines.append(f"**{name}** — usage API returned no data")
            rendered += 1
            continue
        lines.extend(block)
        rendered += 1
    return lines if rendered else []


def _format_day(day: dict[str, Any], label: str) -> tuple[list[str], float]:
    """Return (lines, total_cost) for one daily entry. Lines include a
    header + one line per non-trivial model breakdown."""
    lines: list[str] = []
    total = 0.0
    rows: list[str] = []
    for b in day.get("modelBreakdowns", []):
        cost, parts, short, known = _cost_for_breakdown(b)
        total += cost
        if cost < 0.005:
            continue
        flag = "" if known else " ⚠"
        rows.append(
            f"  {short}{flag}: {_fmt_money(cost)}  "
            f"(in {_fmt_money(parts['in'])} · out {_fmt_money(parts['out'])} · "
            f"cw {_fmt_money(parts['cw'])} · cr {_fmt_money(parts['cr'])})"
        )
    lines.append(f"**{label}** — {_fmt_money(total)}")
    lines.extend(rows)
    return lines, total


def _build_cost_section(now: datetime) -> list[str]:
    try:
        daily = _run_ccusage("daily")
        blocks = _run_ccusage("blocks")
    except FileNotFoundError:
        return ["ccusage not installed (try `npm install -g ccusage`)"]
    except subprocess.TimeoutExpired:
        return [f"ccusage timed out after {_CCUSAGE_TIMEOUT}s"]
    except (RuntimeError, json.JSONDecodeError) as e:
        return [f"ccusage failed: {e}"]

    days = daily.get("daily") or []
    today_iso = now.date().isoformat()
    yest_iso = (now.date() - timedelta(days=1)).isoformat()
    five_days_ago = now.date() - timedelta(days=4)
    by_date = {d.get("date"): d for d in days}

    lines: list[str] = []

    # Today (UTC)
    today = by_date.get(today_iso)
    if today:
        section, today_total = _format_day(today, f"Today ({now.strftime('%b %-d UTC')})")
        lines.extend(section)
    else:
        lines.append(f"**Today ({now.strftime('%b %-d UTC')})** — $0.00")
        today_total = 0.0

    # Yesterday — show when today is sparse (UTC just rolled over).
    if today_total < 5:
        yest = by_date.get(yest_iso)
        if yest:
            yest_label = (now.date() - timedelta(days=1)).strftime("%b %-d UTC")
            section, _ = _format_day(yest, f"Yesterday ({yest_label})")
            lines.append("")
            lines.extend(section)

    # Active block burn rate / projection — straight from ccusage's
    # block-level burnRate (it computes that from local timestamps; we
    # just convert the projected token count into our own dollar amount).
    active = next(
        (b for b in (blocks.get("blocks") or [])
         if b.get("isActive") and not b.get("isGap")),
        None,
    )
    if active is not None:
        a_start = _parse_iso(active["startTime"]).astimezone()
        a_end = _parse_iso(active["endTime"])
        remaining_min = max(0, (a_end - now).total_seconds() / 60)
        burn = active.get("burnRate") or {}
        tpm = burn.get("tokensPerMinute")
        cph = burn.get("costPerHour")
        proj = active.get("projection") or {}
        proj_cost_ccusage = proj.get("totalCost")
        proj_tokens = proj.get("totalTokens")

        # Derive cost-so-far for the active block from its model breakdown
        # if ccusage exposes one; otherwise leave blank rather than show
        # ccusage's possibly-stale costUSD.
        block_cost = 0.0
        for mn in active.get("models") or []:
            # No per-model token split inside a block in ccusage's blocks
            # output; total cost requires summing contributions. We fall
            # back to the totalTokens × price-of-first-model approximation,
            # which is good enough for an active-block burn-rate display.
            pass
        # Simple approach: use the block's per-token-type counts × the
        # primary model's price (works when a block is single-model, which
        # is the common case).
        primary = (active.get("models") or [None])[0]
        if primary:
            price, _short, _known = _price_for(primary)
            tc = active.get("tokenCounts") or {}
            block_cost = (
                (tc.get("inputTokens") or 0)              / 1e6 * price.input
                + (tc.get("outputTokens") or 0)           / 1e6 * price.output
                + (tc.get("cacheCreationInputTokens") or 0) / 1e6 * price.cache_write_5m
                + (tc.get("cacheReadInputTokens") or 0)   / 1e6 * price.cache_read
            )

        lines.append("")
        lines.append(
            f"**Active 5h block** (started {a_start.strftime('%H:%M')}, "
            f"{_fmt_duration(remaining_min)} remaining):"
        )
        lines.append(f"- Cost so far: **{_fmt_money(block_cost)}**")
        if tpm is not None and cph is not None:
            lines.append(
                f"- Burn rate: ~{_fmt_tokens(tpm)} tok/min, "
                f"~{_fmt_money(cph)}/h"
            )
        if proj_cost_ccusage is not None and proj_tokens is not None:
            lines.append(
                f"- Projected by close: ~{_fmt_money(proj_cost_ccusage)} / "
                f"{_fmt_tokens(proj_tokens)} tokens"
            )

    # 5-day rolling — sum daily breakdowns × price table.
    rolling_cost = 0.0
    rolling_tokens = 0
    for d in days:
        try:
            d_date = datetime.strptime(d["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if d_date < five_days_ago:
            continue
        rolling_tokens += d.get("totalTokens") or 0
        for b in d.get("modelBreakdowns", []):
            cost, _, _, _ = _cost_for_breakdown(b)
            rolling_cost += cost
    lines.append("")
    lines.append(
        f"**5-day rolling**: {_fmt_money(rolling_cost)} / "
        f"{_fmt_tokens(rolling_tokens)} tokens"
    )

    return lines


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _build_account_quota_data(name: str, quota: dict[str, Any] | None,
                               now: datetime, *, is_active: bool) -> dict[str, Any]:
    """Structured form of one account's quota — windows + extra-usage."""
    out: dict[str, Any] = {
        "name": name,
        "is_active": is_active,
        "windows": [],
        "extra_usage": None,
        "available": quota is not None,
    }
    if not quota:
        return out
    for key, label in _WINDOW_LABELS:
        win = quota.get(key)
        if not isinstance(win, dict):
            continue
        util = win.get("utilization")
        if util is None:
            continue
        resets = win.get("resets_at")
        out["windows"].append({
            "key": key,
            "label": label,
            "utilization": float(util),
            "resets_at": resets,
            "resets_in_minutes": (
                (_parse_iso(resets) - now).total_seconds() / 60
                if resets else None
            ),
        })
    extra = quota.get("extra_usage") or {}
    if isinstance(extra, dict) and extra.get("is_enabled"):
        # Anthropic returns these in CENTS; convert to dollars so the payload
        # matches the dollar-denominated display. Keys/types are unchanged
        # (ExtraUsage schema + spend dashboard depend on them); monthly_limit
        # stays 0.0 when no cap is set, as before.
        out["extra_usage"] = {
            "used_credits": float(extra.get("used_credits") or 0.0) / 100.0,
            "monthly_limit": float(extra.get("monthly_limit") or 0) / 100.0,
            "currency": extra.get("currency") or "USD",
        }
    return out


def collect_usage_data() -> dict[str, Any]:
    """Structured payload backing both the formatted Discord report and the
    HTTP /api/usage endpoint. All numbers are pre-computed; the formatter
    builds strings out of this dict so the two views stay in lockstep.
    """
    now = datetime.now(timezone.utc)
    accounts_payload: list[dict[str, Any]] = []
    accounts = _discover_accounts()
    active_name = _read_active_account()
    for name, creds_path in accounts:
        oauth = _load_oauth_for(creds_path)
        if oauth is None:
            accounts_payload.append({
                "name": name,
                "is_active": name == active_name,
                "windows": [],
                "extra_usage": None,
                "available": False,
                "error": f"credentials unreadable ({creds_path})",
            })
            continue
        token = oauth.get("accessToken") or ""
        quota = _fetch_quota_with_token(token)
        accounts_payload.append(
            _build_account_quota_data(name, quota, now, is_active=(name == active_name))
        )

    today_payload: dict[str, Any] = {"date": None, "total_cost_usd": 0.0, "models": []}
    active_block_payload: dict[str, Any] | None = None
    rolling_payload: dict[str, Any] = {"days": 5, "cost_usd": 0.0, "tokens": 0}

    try:
        daily = _run_ccusage("daily")
        blocks = _run_ccusage("blocks")
    except (FileNotFoundError, subprocess.TimeoutExpired,
            RuntimeError, json.JSONDecodeError):
        daily = {"daily": []}
        blocks = {"blocks": []}

    days = daily.get("daily") or []
    today_iso = now.date().isoformat()
    five_days_ago = now.date() - timedelta(days=4)
    by_date = {d.get("date"): d for d in days}

    today = by_date.get(today_iso)
    if today:
        today_payload["date"] = today_iso
        for b in today.get("modelBreakdowns", []):
            cost, parts, short, known = _cost_for_breakdown(b)
            today_payload["total_cost_usd"] += cost
            today_payload["models"].append({
                "model": short,
                "is_known_pricing": known,
                "cost_usd": cost,
                "input_cost_usd": parts["in"],
                "output_cost_usd": parts["out"],
                "cache_write_cost_usd": parts["cw"],
                "cache_read_cost_usd": parts["cr"],
            })

    active = next(
        (b for b in (blocks.get("blocks") or [])
         if b.get("isActive") and not b.get("isGap")),
        None,
    )
    if active is not None:
        a_start = _parse_iso(active["startTime"])
        a_end = _parse_iso(active["endTime"])
        burn = active.get("burnRate") or {}
        proj = active.get("projection") or {}
        primary = (active.get("models") or [None])[0]
        block_cost = 0.0
        if primary:
            price, _short, _known = _price_for(primary)
            tc = active.get("tokenCounts") or {}
            block_cost = (
                (tc.get("inputTokens") or 0) / 1e6 * price.input
                + (tc.get("outputTokens") or 0) / 1e6 * price.output
                + (tc.get("cacheCreationInputTokens") or 0) / 1e6 * price.cache_write_5m
                + (tc.get("cacheReadInputTokens") or 0) / 1e6 * price.cache_read
            )
        active_block_payload = {
            "start_time": active["startTime"],
            "end_time": active["endTime"],
            "remaining_minutes": max(0.0, (a_end - now).total_seconds() / 60),
            "cost_usd_so_far": block_cost,
            "tokens_per_minute": burn.get("tokensPerMinute"),
            "cost_per_hour": burn.get("costPerHour"),
            "projected_cost_usd": proj.get("totalCost"),
            "projected_tokens": proj.get("totalTokens"),
        }

    for d in days:
        try:
            d_date = datetime.strptime(d["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if d_date < five_days_ago:
            continue
        rolling_payload["tokens"] += d.get("totalTokens") or 0
        for b in d.get("modelBreakdowns", []):
            cost, _, _, _ = _cost_for_breakdown(b)
            rolling_payload["cost_usd"] += cost

    return {
        "accounts": accounts_payload,
        "today_utc": today_payload,
        "active_block": active_block_payload,
        "rolling_5d": rolling_payload,
    }


def format_usage_report() -> str:
    now = datetime.now(timezone.utc)
    sections: list[list[str]] = []
    quota_section = _build_quota_section(_fetch_anthropic_quota(), now)
    if quota_section:
        sections.append(quota_section)
    sections.append(_build_cost_section(now))
    return "\n\n".join("\n".join(s) for s in sections)


__all__ = ["format_usage_report", "collect_usage_data"]
