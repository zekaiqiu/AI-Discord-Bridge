"""Chat-side account routing: discovery, pick, HOME resolution."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import account_router


@pytest.fixture
def fake_layout(tmp_path, monkeypatch):
    """Create a fake accounts layout under tmp_path and re-point
    account_router at it. Yields ``add(name, util_5h, util_7d)``.
    """
    main_home = tmp_path / "main_home"
    (main_home / ".claude").mkdir(parents=True)
    wiz_root = tmp_path / "wiz" / "claude-accounts"
    wiz_root.mkdir(parents=True)

    # Production excludes ``main`` from the pick pool (CHAT_ACCOUNT_EXCLUDE
    # defaults to "main" — the bridge user's primary plan must not be billed
    # by web-chat sessions). The pool tests below were written with main in
    # the pool, so re-allow it here; the exclude contract has its own tests
    # in the "CHAT_ACCOUNT_EXCLUDE" section.
    monkeypatch.setenv("CHAT_ACCOUNT_EXCLUDE", "")

    monkeypatch.setattr(account_router, "MAIN_HOME", main_home)
    monkeypatch.setattr(
        account_router, "MAIN_CREDENTIALS",
        main_home / ".claude" / ".credentials.json",
    )
    monkeypatch.setattr(account_router, "WIZERITH_ACCOUNTS_ROOT", wiz_root)
    account_router._reset_for_tests()

    fetch_table: dict[str, dict] = {}

    def fake_fetch(token):
        return fetch_table.get(token)
    monkeypatch.setattr(account_router, "_fetch_usage", fake_fetch)

    def add(name: str, *, util_5h: float = 0.0, util_7d: float = 0.0) -> Path:
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
        fetch_table[token] = {
            "five_hour": {"utilization": util_5h, "resets_at": "2099-01-01T00:00:00Z"},
            "seven_day": {"utilization": util_7d, "resets_at": "2099-01-01T00:00:00Z"},
        }
        return home

    yield add
    account_router._reset_for_tests()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_list_accounts_main_only(fake_layout):
    fake_layout("main")
    assert [n for n, _ in account_router.list_accounts()] == ["main"]


def test_list_accounts_main_plus_wizerith_sorted(fake_layout):
    fake_layout("main")
    fake_layout("zzz")
    fake_layout("aaa")
    assert [n for n, _ in account_router.list_accounts()] == ["main", "aaa", "zzz"]


# ---------------------------------------------------------------------------
# Pick
# ---------------------------------------------------------------------------

def test_pick_returns_account_with_lowest_peak(fake_layout):
    fake_layout("main", util_5h=70, util_7d=85)
    fake_layout("account-2", util_5h=10, util_7d=20)
    choice = account_router.pick()
    assert choice.name == "account-2"


def test_pick_skips_accounts_above_80_with_default_threshold(fake_layout):
    # main is at 81% (above the default 80 cutoff); account-2 fine.
    fake_layout("main", util_5h=81)
    fake_layout("account-2", util_5h=10)
    choice = account_router.pick()
    assert choice.name == "account-2"


def test_pick_raises_no_accounts_when_all_saturated(fake_layout):
    fake_layout("main", util_5h=85)         # 5h ≥ 80 cutoff
    fake_layout("account-2", util_7d=100)   # 7d fully consumed
    with pytest.raises(account_router.NoAccountsAvailable):
        account_router.pick()


def test_pick_keeps_account_with_7d_below_100(fake_layout):
    """A 7-day window in the 80–99% band is NOT saturated — it refills weekly
    and every call there is still free plan quota. This is the case that used
    to wrongly exclude main the moment its weekly window crossed 80%."""
    fake_layout("main", util_5h=4, util_7d=80)
    assert account_router.pick().name == "main"
    fake_layout("account-2", util_5h=4, util_7d=99)
    # Both eligible; main has the lower 7d peak so it still wins — the point is
    # account-2 (7d=99) is NOT excluded.
    assert account_router.pick().name == "main"


def test_pick_excludes_7d_only_at_100(fake_layout):
    fake_layout("main", util_5h=4, util_7d=100)   # at the wall → out
    fake_layout("account-2", util_5h=4, util_7d=70)
    assert account_router.pick().name == "account-2"


def test_pick_raises_no_accounts_when_disk_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(account_router, "MAIN_HOME", tmp_path / "no-such")
    monkeypatch.setattr(
        account_router, "MAIN_CREDENTIALS",
        tmp_path / "no-such" / ".claude" / ".credentials.json",
    )
    monkeypatch.setattr(account_router, "WIZERITH_ACCOUNTS_ROOT", tmp_path / "no-wiz")
    account_router._reset_for_tests()
    with pytest.raises(account_router.NoAccountsAvailable):
        account_router.pick()


def test_pick_skips_accounts_with_unfetchable_usage(fake_layout, monkeypatch):
    """While at least one account HAS a usage snapshot, an account whose
    usage can't be fetched is skipped rather than guessed at."""
    fake_layout("main", util_5h=10)
    fake_layout("account-2")
    real = account_router._fetch_usage

    def selective(token):
        if token == "tok-account-2":
            return None
        return real(token)
    monkeypatch.setattr(account_router, "_fetch_usage", selective)

    assert account_router.pick().name == "main"


# ---------------------------------------------------------------------------
# Fail-open: no usage data for ANY candidate (quota API 429 / outage).
# ---------------------------------------------------------------------------

def test_pick_fails_open_to_first_readable_token_when_no_usage_anywhere(
    fake_layout, monkeypatch,
):
    """If the usage API is unreachable for EVERY candidate, pick() must not
    raise — that would drop all new-user provisioning whenever Anthropic's
    quota API rate-limits us. It routes to the first (sorted) candidate with
    a readable credentials file instead."""
    fake_layout("account-3")
    fake_layout("account-2")
    monkeypatch.setattr(account_router, "_fetch_usage", lambda tok: None)
    assert account_router.pick().name == "account-2"


def test_pick_fail_open_never_returns_an_account_in_live_429_cooldown(
    fake_layout, monkeypatch,
):
    """An observed 429 (mark_hot) is stronger than "usage unavailable": a
    hot account is never a fail-open candidate. Because the hot account still
    counts as a discovered candidate, fail-open (which requires usage to be
    missing for EVERY candidate) does not trigger at all — pick() raises
    rather than routing anywhere."""
    fake_layout("account-2")
    fake_layout("account-3")
    monkeypatch.setattr(account_router, "_fetch_usage", lambda tok: None)
    account_router.mark_hot("account-2", until_ts=time.time() + 300)
    with pytest.raises(account_router.NoAccountsAvailable):
        account_router.pick()
    # Once the cooldown clears, fail-open resumes and skips nothing.
    account_router.note_success("account-2")
    assert account_router.pick().name == "account-2"


def test_pick_does_not_fail_open_when_some_usage_is_known(fake_layout, monkeypatch):
    """Fail-open is only for "no data at all". If one account reported
    usage and is saturated while the other is unfetchable, that is a
    genuine no-usable-account situation."""
    fake_layout("account-2", util_5h=95)
    fake_layout("account-3")
    real = account_router._fetch_usage

    def selective(token):
        return None if token == "tok-account-3" else real(token)
    monkeypatch.setattr(account_router, "_fetch_usage", selective)
    with pytest.raises(account_router.NoAccountsAvailable):
        account_router.pick()


# ---------------------------------------------------------------------------
# CHAT_ACCOUNT_EXCLUDE: ``main`` is out of the pick pool by default.
# ---------------------------------------------------------------------------

def test_list_accounts_excludes_main_by_default(fake_layout, monkeypatch):
    monkeypatch.delenv("CHAT_ACCOUNT_EXCLUDE", raising=False)
    fake_layout("main")
    fake_layout("account-2")
    assert [n for n, _ in account_router.list_accounts()] == ["account-2"]


def test_pick_never_returns_main_by_default(fake_layout, monkeypatch):
    """Even when main has the most headroom it is not billable by chat."""
    monkeypatch.delenv("CHAT_ACCOUNT_EXCLUDE", raising=False)
    fake_layout("main", util_5h=0)
    fake_layout("account-2", util_5h=60)
    assert account_router.pick().name == "account-2"


def test_pick_raises_when_only_main_exists_by_default(fake_layout, monkeypatch):
    monkeypatch.delenv("CHAT_ACCOUNT_EXCLUDE", raising=False)
    fake_layout("main")
    with pytest.raises(account_router.NoAccountsAvailable):
        account_router.pick()


def test_exclude_env_is_a_comma_list_of_names(fake_layout, monkeypatch):
    monkeypatch.setenv("CHAT_ACCOUNT_EXCLUDE", "main, account-2")
    fake_layout("main")
    fake_layout("account-2")
    fake_layout("account-3")
    assert [n for n, _ in account_router.list_accounts()] == ["account-3"]


def test_home_for_account_ignores_exclude_filter(fake_layout, monkeypatch):
    """The exclude list gates NEW picks only. Sessions already locked to
    ``main`` must keep resolving to its HOME (otherwise their credential
    refresh would break mid-conversation)."""
    monkeypatch.delenv("CHAT_ACCOUNT_EXCLUDE", raising=False)
    fake_layout("main")
    assert account_router.home_for_account("main") == account_router.MAIN_HOME
    assert account_router.is_usable("main") is True


# ---------------------------------------------------------------------------
# Hermeticity: the only network seam is ``_fetch_usage`` and the suite-wide
# guard trips it. Every routing test in this file therefore ran on the
# canned/faked snapshot, never the live usage API.
# ---------------------------------------------------------------------------

def test_real_fetch_usage_is_blocked_by_the_no_network_guard(hermetic_accounts):
    with pytest.raises(AssertionError, match="network access attempted"):
        hermetic_accounts.real_fetch_usage("tok-anything")


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def test_snapshot_caches_within_ttl(fake_layout, monkeypatch):
    fake_layout("main", util_5h=10)
    main_home = account_router.MAIN_HOME
    calls = {"n": 0}
    real = account_router._fetch_usage

    def counting(token):
        calls["n"] += 1
        return real(token)
    monkeypatch.setattr(account_router, "_fetch_usage", counting)

    a = account_router.snapshot_usage("main", main_home)
    b = account_router.snapshot_usage("main", main_home)
    assert a is not None and a == b
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# home_for_account resolution
# ---------------------------------------------------------------------------

def test_home_for_account_main_returns_main_home(fake_layout):
    fake_layout("main")
    assert account_router.home_for_account("main") == account_router.MAIN_HOME


def test_home_for_account_none_returns_main_home(fake_layout):
    fake_layout("main")
    assert account_router.home_for_account(None) == account_router.MAIN_HOME


def test_home_for_account_known_returns_wizerith_dir(fake_layout):
    fake_layout("main")
    fake_layout("account-2")
    assert (
        account_router.home_for_account("account-2")
        == account_router.WIZERITH_ACCOUNTS_ROOT / "account-2"
    )


def test_home_for_account_unknown_falls_back_to_main(fake_layout):
    fake_layout("main")
    # No account-3 exists.
    assert (
        account_router.home_for_account("account-3")
        == account_router.MAIN_HOME
    )


# ---------------------------------------------------------------------------
# is_usable — token-level + plan-level usability probe
# ---------------------------------------------------------------------------

def test_is_usable_true_for_fresh_account(fake_layout):
    fake_layout("account-2", util_5h=10.0)
    assert account_router.is_usable("account-2") is True


def test_is_usable_false_when_5h_saturated(fake_layout):
    fake_layout("account-2", util_5h=100.0)
    assert account_router.is_usable("account-2") is False


def test_is_usable_false_when_token_expired(fake_layout):
    home = fake_layout("account-2", util_5h=0.0)
    creds = home / ".claude" / ".credentials.json"
    data = json.loads(creds.read_text())
    data["claudeAiOauth"]["expiresAt"] = int((time.time() - 60) * 1000)
    creds.write_text(json.dumps(data))
    assert account_router.is_usable("account-2") is False


def test_is_usable_false_when_credentials_missing(fake_layout):
    fake_layout("account-2")
    # Unknown name falls back to MAIN_HOME, which has no credentials file.
    assert account_router.is_usable("nonexistent") is False


def test_is_usable_true_when_usage_api_unreachable(fake_layout, monkeypatch):
    """Quota-API outage must not disqualify a token-valid account —
    failing closed there would wedge every container at once."""
    fake_layout("account-2")
    account_router._reset_for_tests()
    monkeypatch.setattr(account_router, "_fetch_usage", lambda tok: None)
    assert account_router.is_usable("account-2") is True


def test_is_usable_false_when_7d_at_100(fake_layout):
    fake_layout("account-2", util_5h=10.0, util_7d=100.0)
    assert account_router.is_usable("account-2") is False
