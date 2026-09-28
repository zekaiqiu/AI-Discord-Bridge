"""Bridge-side multi-account routing: discovery, pick, persistent state,
auto-switch on saturation."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import bridge_account_router as router


@pytest.fixture
def fake_layout(tmp_path, monkeypatch):
    """Build a fake accounts layout (main + wizerith) plus pristine
    state files under tmp_path; rebind module constants to it. Yields
    ``add(name, util_5h, util_7d)``."""
    main_home = tmp_path / "main_home"
    (main_home / ".claude").mkdir(parents=True)
    wiz_root = tmp_path / "wiz" / "claude-accounts"
    wiz_root.mkdir(parents=True)
    bridge_state = tmp_path / "bridge-active"
    display_state = tmp_path / "display-active"
    last_run_state = tmp_path / "bridge-last-run"

    monkeypatch.setattr(router, "MAIN_HOME", main_home)
    monkeypatch.setattr(
        router, "MAIN_CREDENTIALS",
        main_home / ".claude" / ".credentials.json",
    )
    monkeypatch.setattr(router, "WIZERITH_ACCOUNTS_ROOT", wiz_root)
    monkeypatch.setattr(router, "BRIDGE_ACTIVE_FILE", bridge_state)
    monkeypatch.setattr(router, "DISPLAY_ACTIVE_FILE", display_state)
    monkeypatch.setattr(router, "BRIDGE_LAST_RUN_FILE", last_run_state)
    # Default sticky window to 0 so existing switch tests behave as
    # before — individual tests for stickiness override this.
    monkeypatch.setattr(router, "STICKY_IDLE_SECONDS", 0.0)
    # Disable the emergency qwen provider by default so existing tests exercise
    # the Anthropic-only behavior. The real resolver would otherwise find the
    # key in portfolio-tool's .env on the host. Qwen tests re-enable it.
    monkeypatch.setattr(router, "resolve_haihub_key", lambda: None)
    router._reset_for_tests()

    fetch_table: dict[str, dict] = {}
    dead_tokens: set[str] = set()  # tokens that probe as a 401/403 (auth dead)

    def fake_fetch_status(token):
        if token in dead_tokens:
            return None, router.USAGE_AUTH_DEAD
        snap = fetch_table.get(token)
        if snap is None:
            return None, router.USAGE_UNREACHABLE
        return snap, router.USAGE_OK
    monkeypatch.setattr(router, "_fetch_usage_status", fake_fetch_status)

    def fake_fetch(token):
        return fetch_table.get(token)
    monkeypatch.setattr(router, "_fetch_usage", fake_fetch)

    def add(
        name: str,
        *,
        util_5h: float = 0.0,
        util_7d: float = 0.0,
        overage_used_cents: float | None = None,
        overage_cap_cents: float | None = None,
    ) -> Path:
        """Register a fake account. ``overage_used_cents`` (and optionally
        ``overage_cap_cents``) attach an ``extra_usage`` block mirroring the
        raw /oauth/usage shape — used_credits/monthly_limit are CENTS, as the
        router sees them pre-normalization."""
        token = f"tok-{name}"
        if name == "main":
            home = main_home
        else:
            home = wiz_root / name
            (home / ".claude").mkdir(parents=True)
        creds = home / ".claude" / ".credentials.json"
        creds.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": token,
                "refreshToken": f"refresh-{name}",
                "expiresAt": int((time.time() + 3600) * 1000),
            }
        }))
        snap: dict = {
            "five_hour": {"utilization": util_5h, "resets_at": "2099-01-01T00:00:00Z"},
            "seven_day": {"utilization": util_7d, "resets_at": "2099-01-01T00:00:00Z"},
        }
        if overage_used_cents is not None:
            snap["extra_usage"] = {
                "is_enabled": True,
                "used_credits": overage_used_cents,
                "monthly_limit": overage_cap_cents,
                "currency": "USD",
            }
        fetch_table[token] = snap
        return home

    def mark_dead(name: str) -> None:
        """Flip a registered account's token to probe as a 401 (auth dead)."""
        dead_tokens.add(f"tok-{name}")
        fetch_table.pop(f"tok-{name}", None)
    add.mark_dead = mark_dead  # type: ignore[attr-defined]

    def mark_unreachable(name: str) -> None:
        """Make an account's probe fail transiently (network/timeout, NOT a 401):
        token stays readable, snapshot just can't be fetched."""
        fetch_table.pop(f"tok-{name}", None)  # not in table, not dead → UNREACHABLE
    add.mark_unreachable = mark_unreachable  # type: ignore[attr-defined]

    yield add
    router._reset_for_tests()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_list_accounts_main_only(fake_layout):
    fake_layout("main")
    assert [n for n, _ in router.list_accounts()] == ["main"]


def test_list_accounts_main_plus_wizerith_sorted(fake_layout):
    fake_layout("main")
    fake_layout("zzz")
    fake_layout("aaa")
    assert [n for n, _ in router.list_accounts()] == ["main", "aaa", "zzz"]


# ---------------------------------------------------------------------------
# Pick
# ---------------------------------------------------------------------------

def test_pick_returns_lowest_peak(fake_layout):
    fake_layout("main", util_5h=70, util_7d=85)
    fake_layout("account-2", util_5h=10, util_7d=20)
    assert router.pick().name == "account-2"


def test_pick_skips_above_80_with_default_threshold(fake_layout):
    fake_layout("main", util_5h=81)
    fake_layout("account-2", util_5h=10)
    assert router.pick().name == "account-2"


def test_pick_raises_when_all_saturated(fake_layout):
    fake_layout("main", util_5h=85)        # 5h ≥ 80 cutoff
    fake_layout("account-2", util_7d=100)  # 7d fully consumed
    with pytest.raises(router.NoAccountsAvailable):
        router.pick()


# ---------------------------------------------------------------------------
# 7-day saturation cutoff — weekly windows only retire at a *full* 100%,
# while the fast 5h window still retires at the 80% headroom cutoff.
# ---------------------------------------------------------------------------

def test_pick_keeps_account_with_7d_in_80s(fake_layout):
    """A 7-day window in the 80–99% band is NOT saturated — it's a week-long
    budget and every call is still free plan quota. Only 100% retires it. This
    is the case that used to wrongly exclude the bridge's own main account the
    moment its weekly window crossed 80%."""
    fake_layout("main", util_5h=4, util_7d=80)
    assert router.pick().name == "main"
    # Even right up against the wall (99%) it stays eligible.
    fake_layout("account-2", util_5h=4, util_7d=99)
    # main has the lower 7d peak, so it still wins; the point is account-2 is
    # not excluded — drop main below it and account-2 gets picked.
    assert router.pick().name == "main"


def test_pick_excludes_7d_only_at_100(fake_layout):
    fake_layout("main", util_5h=4, util_7d=100)   # exactly at the wall → out
    fake_layout("account-2", util_5h=4, util_7d=70)
    assert router.pick().name == "account-2"


def test_is_plan_saturated_per_window_thresholds():
    cutoff = 80.0
    # 5h retires at the cutoff; 7d does not until 100.
    assert router.is_plan_saturated({"five_hour": {"utilization": 80}}, cutoff) is True
    assert router.is_plan_saturated({"five_hour": {"utilization": 79}}, cutoff) is False
    assert router.is_plan_saturated({"seven_day": {"utilization": 99}}, cutoff) is False
    assert router.is_plan_saturated({"seven_day": {"utilization": 100}}, cutoff) is True
    assert router.is_plan_saturated(None, cutoff) is False
    assert router.is_plan_saturated({}, cutoff) is False


# ---------------------------------------------------------------------------
# Extra-usage (overage) — the cap must NOT matter while plan quota has free
# headroom. Overage only bills once plan is exhausted, so an account with a
# fresh 5h/7d window serves the next call for $0 regardless of past spend.
# ---------------------------------------------------------------------------

def test_is_account_in_overage_detects_spend():
    assert router.is_account_in_overage(
        {"extra_usage": {"is_enabled": True, "used_credits": 9379, "monthly_limit": 10000}}
    ) is True


def test_is_account_in_overage_false_when_zero_or_disabled():
    assert router.is_account_in_overage(
        {"extra_usage": {"is_enabled": True, "used_credits": 0, "monthly_limit": 10000}}
    ) is False
    assert router.is_account_in_overage(
        {"extra_usage": {"is_enabled": False, "used_credits": 9999, "monthly_limit": 10000}}
    ) is False
    assert router.is_account_in_overage(None) is False
    assert router.is_account_in_overage({}) is False


def test_pick_ignores_overage_when_plan_has_headroom(fake_layout):
    """An account at its overage cap but with a fresh plan window is the
    *freshest* free option. The cap must not exclude it — the next call draws
    from plan at $0. Lowest plan peak wins, overage notwithstanding."""
    fake_layout("main", util_5h=40)
    fake_layout(
        "account-2", util_5h=5, util_7d=10,
        overage_used_cents=10000, overage_cap_cents=10000,
    )
    assert router.pick().name == "account-2"


def test_pick_returns_overage_account_when_it_is_only_candidate(fake_layout):
    """A single account with plan headroom is usable even if it has spent into
    overage this month — it does NOT raise NoAccountsAvailable. Plan headroom,
    not overage state, decides eligibility."""
    fake_layout("main", util_5h=10, overage_used_cents=500, overage_cap_cents=10000)
    assert router.pick().name == "main"


def test_pick_excludes_overage_account_only_when_plan_saturated(fake_layout):
    """Overage still effectively keeps an account out once its plan is
    exhausted — but the exclusion is on the plan-saturation basis, not the
    overage flag. Here account-2 is 5h-saturated, so main wins."""
    fake_layout("main", util_5h=33)
    fake_layout(
        "account-2", util_5h=90, util_7d=52,
        overage_used_cents=10000, overage_cap_cents=10000,
    )
    assert router.pick().name == "main"


def test_bound_overage_account_with_plan_headroom_stays(fake_layout):
    """Core of the new behavior: bound to an overage-capped account whose plan
    window still has headroom. We do NOT churn --continue to chase a 'cleaner'
    account — the next call is free plan quota, so staying costs nothing."""
    fake_layout("main", util_5h=33)
    fake_layout(
        "account-2", util_5h=22, util_7d=52,
        overage_used_cents=10000, overage_cap_cents=10000,
    )
    router.write_current_account("account-2")
    decision = router.resolve_account_for_run()
    assert decision.name == "account-2"
    assert decision.switched is False
    assert router.read_current_account() == "account-2"


def test_overage_does_not_break_sticky(fake_layout, monkeypatch):
    """With plan headroom there is no re-evaluation at all, so overage never
    forces a switch — inside or outside the sticky window."""
    fake_layout("main", util_5h=33)
    fake_layout("account-2", util_5h=22, overage_used_cents=9000, overage_cap_cents=10000)
    router.write_current_account("account-2")
    monkeypatch.setattr(router, "STICKY_IDLE_SECONDS", 900.0)
    router.record_last_run(time.time() - 30)
    decision = router.resolve_account_for_run()
    assert decision.name == "account-2"
    assert decision.switched is False


def test_overage_account_does_not_route_to_qwen_with_plan_headroom(fake_layout, with_qwen):
    """Even with a qwen key available, an overage account that still has plan
    headroom is served from plan — qwen is only for genuine plan saturation."""
    fake_layout("main", util_5h=30, overage_used_cents=8000, overage_cap_cents=10000)
    fake_layout("account-2", util_5h=20, overage_used_cents=9000, overage_cap_cents=10000)
    router.write_current_account("account-2")
    decision = router.resolve_account_for_run()
    assert decision.provider == "anthropic"
    assert decision.name == "account-2"
    assert decision.switched is False


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

def test_read_current_account_returns_none_when_unwritten(fake_layout):
    assert router.read_current_account() is None


def test_write_then_read_current_account(fake_layout):
    router.write_current_account("account-2")
    assert router.read_current_account() == "account-2"


def test_record_active_writes_display_marker(fake_layout):
    router.record_active("account-2")
    assert router.DISPLAY_ACTIVE_FILE.read_text() == "account-2"


# ---------------------------------------------------------------------------
# resolve_account_for_run — the real driver bot.run_claude calls.
# ---------------------------------------------------------------------------

def test_first_run_picks_lowest_peak_and_persists(fake_layout):
    fake_layout("main", util_5h=10)
    fake_layout("account-2", util_5h=5)
    decision = router.resolve_account_for_run()
    assert decision.name == "account-2"
    assert decision.switched is False  # no previous → first-run, not a switch
    assert decision.previous is None
    assert router.read_current_account() == "account-2"


def test_persisted_account_kept_when_under_cutoff(fake_layout):
    fake_layout("main", util_5h=10)
    fake_layout("account-2", util_5h=5)
    router.write_current_account("main")
    decision = router.resolve_account_for_run()
    # Even though account-2 has lower peak, we don't switch off main
    # while it's still under the cutoff. Stable account binding is the
    # whole point of the persistence.
    assert decision.name == "main"
    assert decision.switched is False


def test_auto_switch_when_persisted_account_saturated(fake_layout):
    fake_layout("main", util_5h=85)
    fake_layout("account-2", util_5h=5)
    router.write_current_account("main")
    decision = router.resolve_account_for_run()
    assert decision.name == "account-2"
    assert decision.switched is True
    assert decision.previous == "main"
    # Persistence updated.
    assert router.read_current_account() == "account-2"
    # Display marker updated.
    assert router.DISPLAY_ACTIVE_FILE.read_text() == "account-2"


def test_sticky_keeps_saturated_account_within_idle_window(fake_layout, monkeypatch):
    """Auto-switch is suppressed if the previous claude run was recent —
    we'd rather risk a 429 than lose --continue context mid-debug."""
    fake_layout("main", util_5h=85)
    fake_layout("account-2", util_5h=5)
    router.write_current_account("main")
    # Pretend a claude run just happened 30s ago and the sticky window
    # is 15 minutes.
    monkeypatch.setattr(router, "STICKY_IDLE_SECONDS", 900.0)
    router.record_last_run(time.time() - 30)
    decision = router.resolve_account_for_run()
    assert decision.name == "main", "should stay sticky on bound account"
    assert decision.switched is False
    # Persistence unchanged.
    assert router.read_current_account() == "main"


def test_sticky_releases_after_idle_window_expires(fake_layout, monkeypatch):
    """After the user has been idle past the sticky window, the next
    message picks up the saturation cutoff and switches normally."""
    fake_layout("main", util_5h=85)
    fake_layout("account-2", util_5h=5)
    router.write_current_account("main")
    monkeypatch.setattr(router, "STICKY_IDLE_SECONDS", 900.0)
    # Last run was 20 minutes ago — outside the 15-minute sticky window.
    router.record_last_run(time.time() - 1200)
    decision = router.resolve_account_for_run()
    assert decision.name == "account-2"
    assert decision.switched is True


def test_sticky_skipped_on_first_run(fake_layout, monkeypatch):
    """No last-run timestamp means the bridge just started (or this is
    the very first message); switch as normal — there is no live
    conversation context to preserve."""
    fake_layout("main", util_5h=85)
    fake_layout("account-2", util_5h=5)
    router.write_current_account("main")
    monkeypatch.setattr(router, "STICKY_IDLE_SECONDS", 900.0)
    # No record_last_run() — file does not exist.
    decision = router.resolve_account_for_run()
    assert decision.name == "account-2"
    assert decision.switched is True


def test_ignore_sticky_switches_off_saturated_within_idle_window(fake_layout, monkeypatch):
    """A fresh session (ignore_sticky=True) must abandon a saturated bound
    account even if the last run was recent — there is no --continue history
    to protect. This is the !new-doesn't-rotate bug that pinned the bridge to
    a 100% account through a long conversation."""
    fake_layout("main", util_5h=85)
    fake_layout("account-2", util_5h=5)
    router.write_current_account("main")
    monkeypatch.setattr(router, "STICKY_IDLE_SECONDS", 900.0)
    router.record_last_run(time.time() - 30)  # well within sticky window
    decision = router.resolve_account_for_run(ignore_sticky=True)
    assert decision.name == "account-2"
    assert decision.switched is True
    assert decision.previous == "main"


def test_ignore_sticky_keeps_healthy_account_no_flap(fake_layout, monkeypatch):
    """ignore_sticky must NOT re-pick when the bound account is healthy —
    otherwise fresh sessions would flap between near-tied accounts (the
    reason force_pick is avoided on !new)."""
    fake_layout("main", util_5h=10)
    fake_layout("account-2", util_5h=5)
    router.write_current_account("main")
    monkeypatch.setattr(router, "STICKY_IDLE_SECONDS", 900.0)
    router.record_last_run(time.time() - 30)
    decision = router.resolve_account_for_run(ignore_sticky=True)
    assert decision.name == "main"
    assert decision.switched is False


def test_hard_ceiling_overrides_sticky_mid_conversation(fake_layout, monkeypatch):
    """Even on a continuing session within the sticky window, an account at or
    above HARD_SWITCH_PCT is abandoned — a 429 is imminent and would fail the
    turn anyway. Backstop against a hot account getting pinned at ~100%."""
    fake_layout("main", util_5h=97)   # >= hard ceiling
    fake_layout("account-2", util_5h=5)
    router.write_current_account("main")
    monkeypatch.setattr(router, "STICKY_IDLE_SECONDS", 900.0)
    monkeypatch.setattr(router, "HARD_SWITCH_PCT", 95.0)
    router.record_last_run(time.time() - 30)  # recent — sticky would normally hold
    decision = router.resolve_account_for_run()  # continuing session
    assert decision.name == "account-2"
    assert decision.switched is True


def test_soft_band_still_sticky_below_hard_ceiling(fake_layout, monkeypatch):
    """Between the saturation cutoff and the hard ceiling, sticky still holds a
    continuing session to preserve --continue context."""
    fake_layout("main", util_5h=85)   # in soft band [80, 95)
    fake_layout("account-2", util_5h=5)
    router.write_current_account("main")
    monkeypatch.setattr(router, "STICKY_IDLE_SECONDS", 900.0)
    monkeypatch.setattr(router, "HARD_SWITCH_PCT", 95.0)
    router.record_last_run(time.time() - 30)
    decision = router.resolve_account_for_run()
    assert decision.name == "main"
    assert decision.switched is False


def test_resolve_stamps_last_run(fake_layout):
    """Every successful resolution updates the last-run marker so the
    next call can do the idle-window math."""
    fake_layout("main", util_5h=10)
    before = time.time()
    router.resolve_account_for_run()
    stamped = router.read_last_run()
    assert stamped is not None
    # record_last_run persists with .3f precision, so a full-precision `before`
    # can round just above the stored value — allow a 1ms tolerance.
    assert before - 0.001 <= stamped <= time.time() + 1


def test_force_pick_repicks_even_when_persisted_is_healthy(fake_layout):
    fake_layout("main", util_5h=10)
    fake_layout("account-2", util_5h=5)
    router.write_current_account("main")
    # Force-pick simulates !newsession — caller wants a fresh account.
    decision = router.resolve_account_for_run(force_pick=True)
    assert decision.name == "account-2"
    # ``switched`` is True because the previously-persisted account
    # differed from the new pick; bot.run_claude uses this to drop
    # ``--continue`` (the new HOME has no prior session anyway).
    assert decision.switched is True
    assert decision.previous == "main"


def test_all_saturated_keeps_previous_account_no_switch(fake_layout):
    """If every account is over the cutoff, stay on the persisted one
    rather than gratuitously starting a fresh session — the rate-limit
    will surface naturally on the next subprocess call."""
    fake_layout("main", util_5h=85)
    fake_layout("account-2", util_5h=92)
    router.write_current_account("main")
    decision = router.resolve_account_for_run()
    assert decision.name == "main"
    assert decision.switched is False


def test_unfetchable_persisted_account_does_not_force_switch(fake_layout, monkeypatch):
    """Transient API failure on the usage probe must NOT cause a session
    reset. Stay on the persisted account; the actual API call will get
    its own response from anthropic."""
    fake_layout("main", util_5h=10)
    fake_layout("account-2", util_5h=5)
    router.write_current_account("main")
    monkeypatch.setattr(router, "_fetch_usage", lambda token: None)
    decision = router.resolve_account_for_run()
    assert decision.name == "main"
    assert decision.switched is False


# ---------------------------------------------------------------------------
# Emergency qwen provider — only when every reachable Anthropic account is
# saturated AND a haihub key is resolvable.
# ---------------------------------------------------------------------------

@pytest.fixture
def with_qwen(monkeypatch):
    """Enable the emergency provider by stubbing a resolvable key."""
    monkeypatch.setattr(router, "resolve_haihub_key", lambda: "tok-haihub")
    return None


def test_all_saturated_routes_to_qwen_when_key_present(fake_layout, with_qwen):
    fake_layout("main", util_5h=85)
    fake_layout("account-2", util_5h=92)
    router.write_current_account("main")
    decision = router.resolve_account_for_run()
    assert decision.provider == "qwen"
    assert decision.name == router.QWEN_EMERGENCY_NAME
    assert decision.switched is True
    assert decision.previous == "main"
    # Persisted so the next turn knows it's on the fallback.
    assert router.read_current_account() == router.QWEN_EMERGENCY_NAME


def test_all_saturated_stays_anthropic_when_no_key(fake_layout):
    """Default fixture has no key → preserve the old limp-on-previous behavior."""
    fake_layout("main", util_5h=85)
    fake_layout("account-2", util_5h=92)
    router.write_current_account("main")
    decision = router.resolve_account_for_run()
    assert decision.provider == "anthropic"
    assert decision.name == "main"
    assert decision.switched is False


def test_qwen_not_used_while_a_healthy_account_exists(fake_layout, with_qwen):
    """Emergency provider must NOT trigger if any account is under the cutoff."""
    fake_layout("main", util_5h=85)
    fake_layout("account-2", util_5h=5)
    router.write_current_account("main")
    decision = router.resolve_account_for_run()
    assert decision.provider == "anthropic"
    assert decision.name == "account-2"


def test_first_run_all_saturated_routes_to_qwen(fake_layout, with_qwen):
    fake_layout("main", util_5h=85)
    fake_layout("account-2", util_5h=99)
    decision = router.resolve_account_for_run()  # no persisted account
    assert decision.provider == "qwen"
    assert decision.previous is None


def test_recovers_off_qwen_when_account_frees_up(fake_layout, with_qwen):
    """Bound to qwen, an Anthropic account drops below the cutoff → climb back."""
    fake_layout("main", util_5h=10)
    fake_layout("account-2", util_5h=5)
    router.write_current_account(router.QWEN_EMERGENCY_NAME)
    decision = router.resolve_account_for_run()
    assert decision.provider == "anthropic"
    assert decision.name == "account-2"  # lowest peak
    assert decision.switched is True
    assert decision.previous == router.QWEN_EMERGENCY_NAME


def test_stays_on_qwen_while_all_still_saturated(fake_layout, with_qwen):
    fake_layout("main", util_5h=88)
    fake_layout("account-2", util_5h=95)
    router.write_current_account(router.QWEN_EMERGENCY_NAME)
    decision = router.resolve_account_for_run()
    assert decision.provider == "qwen"
    assert decision.name == router.QWEN_EMERGENCY_NAME


def test_resolve_haihub_key_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HAIHUB_API_KEY", "env-key")
    assert router.resolve_haihub_key() == "env-key"


def test_resolve_haihub_key_falls_back_to_file(monkeypatch, tmp_path):
    monkeypatch.delenv("HAIHUB_API_KEY", raising=False)
    envfile = tmp_path / ".env"
    envfile.write_text('FOO=bar\nHAIHUB_API_KEY="file-key"\nBAZ=qux\n')
    monkeypatch.setattr(router, "HAIHUB_ENV_FALLBACK_FILE", envfile)
    assert router.resolve_haihub_key() == "file-key"


def test_resolve_haihub_key_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("HAIHUB_API_KEY", raising=False)
    monkeypatch.setattr(router, "HAIHUB_ENV_FALLBACK_FILE", tmp_path / "nope.env")
    assert router.resolve_haihub_key() is None


def test_unknown_persisted_account_falls_back_to_main(fake_layout):
    """If someone hand-edited the state file to reference an account
    that no longer exists on disk, home_for_account falls back to main
    so the next subprocess at least lands on a real credentials file."""
    fake_layout("main")
    router.write_current_account("ghost-account")
    decision = router.resolve_account_for_run()
    # The persisted name ("ghost-account") is kept in the decision so a
    # caller logging the name sees ground truth, but home_path resolves
    # to main's home (the safe fallback).
    assert decision.name == "ghost-account"
    assert decision.home_path == router.MAIN_HOME


# ---------------------------------------------------------------------------
# Auth failover: a bound account whose token is rejected (401/403) must switch
# to a working account; a transient probe failure must NOT churn the binding.
# ---------------------------------------------------------------------------

def test_failover_when_bound_account_auth_dead(fake_layout):
    fake_layout("main", util_5h=3)
    fake_layout("account-2", util_5h=1)
    router.write_current_account("main")          # bound to main
    fake_layout.mark_dead("main")                 # main's token now 401s
    decision = router.resolve_account_for_run()
    assert decision.name == "account-2"           # failed over
    assert decision.switched is True
    assert decision.previous == "main"
    # binding moved so the next turn doesn't re-pick the dead account
    assert router.read_current_account() == "account-2"


def test_no_switch_when_bound_account_probe_unreachable(fake_layout):
    fake_layout("main", util_5h=3)
    fake_layout("account-2", util_5h=1)
    router.write_current_account("main")
    fake_layout.mark_unreachable("main")          # transient network failure, NOT a 401
    decision = router.resolve_account_for_run()
    assert decision.name == "main"                # kept — don't churn on a hiccup
    assert decision.switched is False


def test_force_pick_skips_auth_dead_account(fake_layout):
    fake_layout("main", util_5h=1)                # lowest peak — would normally win
    fake_layout("account-2", util_5h=5)
    fake_layout.mark_dead("main")                 # but main's token is dead
    decision = router.resolve_account_for_run(force_pick=True)
    assert decision.name == "account-2"           # pick() skipped the dead one
