"""Volume/network labels, env-driven limits, override JSON.

No real docker, no real subprocess; all docker calls go through MagicMock.
"""

import json
from unittest.mock import MagicMock

import docker.errors
import pytest

import user_container
from user_container import (
    CPUS_ENV,
    DEFAULT_CPUS,
    DEFAULT_MEM,
    MEM_ENV,
    NETWORK_LABEL_KEY,
    OVERRIDES_DIR_ENV,
    VOLUME_LABEL_KEY,
    _hash_for,
    container_name_for,
    ensure_user_container,
)

EMAIL = "phase3@example.com"
HASH = _hash_for(EMAIL)
NAME = container_name_for(EMAIL)
VOL = f"{NAME}-home"
NET = f"{NAME}-net"



# ---------------------------------------------------------------------------
# Item 1: stub the in-container exec seam so ensure_user_container's new
# post-up provisioning calls don't try to reach a real docker daemon during
# tests that already mock the docker client.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_docker_exec(monkeypatch, tmp_path):
    class _CP:
        def __init__(self, rc=0, out=b""):
            self.returncode = rc
            self.stdout = out
            self.stderr = b""

    def _fake_run(argv, *, input=None, capture_output=True, check=True):
        # `stat` returns "missing" so refresh_credentials_if_stale would
        # populate; populate_credentials needs a readable source file.
        if any(a == "stat" for a in argv) and "-c" in argv:
            return _CP(rc=1)
        return _CP(rc=0)

    monkeypatch.setattr(user_container.subprocess, "run", _fake_run)
    fake_root = tmp_path / "claude-accounts"
    (fake_root / "account-1" / ".claude").mkdir(parents=True)
    (fake_root / "account-1" / ".claude" / ".credentials.json").write_bytes(b"{}")
    monkeypatch.setattr(user_container, "CLAUDE_ACCOUNTS_HOST_PATH", str(fake_root))
    # Item 3: redirect the allocator's persistence file off /data/.
    alloc_path = tmp_path / "user-network-allocations.json"
    monkeypatch.setenv(user_container.USER_NETWORK_ALLOCATIONS_ENV, str(alloc_path))
    user_container._REFRESH_CACHE.clear()
    yield
    user_container._REFRESH_CACHE.clear()


def _make_client(
    container_present: bool = False,
    network_present: bool = False,
    volume_present: bool = False,
):
    client = MagicMock()
    if container_present:
        client.containers.get.return_value = MagicMock()
    else:
        client.containers.get.side_effect = docker.errors.NotFound("missing")
    if network_present:
        client.networks.get.return_value = MagicMock()
    else:
        client.networks.get.side_effect = docker.errors.NotFound("missing")
    if volume_present:
        client.volumes.get.return_value = MagicMock()
    else:
        client.volumes.get.side_effect = docker.errors.NotFound("missing")
    return client


def _run_kwargs(client):
    return client.containers.run.call_args.kwargs


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Each test starts with a clean env and an isolated overrides dir."""
    monkeypatch.delenv(MEM_ENV, raising=False)
    monkeypatch.delenv(CPUS_ENV, raising=False)
    monkeypatch.setenv(OVERRIDES_DIR_ENV, str(tmp_path))
    return tmp_path


# --- Group A: volume hardening -----------------------------------------

def test_volume_created_when_missing():
    client = _make_client()
    ensure_user_container(EMAIL, client=client)
    assert client.volumes.create.call_count == 1
    kw = client.volumes.create.call_args.kwargs
    assert kw["name"] == VOL
    assert kw["labels"] == {VOLUME_LABEL_KEY: HASH}


def test_volume_reused_when_present():
    client = _make_client(volume_present=True)
    ensure_user_container(EMAIL, client=client)
    assert client.volumes.create.call_count == 0


def test_volume_persists_across_container_recreation():
    client = MagicMock()
    # Both calls: container missing → triggers run().
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    # Networks present so we don't recreate.
    client.networks.get.return_value = MagicMock()
    # Volume: missing on first call, present on second.
    client.volumes.get.side_effect = [
        docker.errors.NotFound("missing"),
        MagicMock(),
    ]

    ensure_user_container(EMAIL, client=client)
    first_vol = list(client.containers.run.call_args.kwargs["volumes"].keys())[0]

    ensure_user_container(EMAIL, client=client)
    second_vol = list(client.containers.run.call_args.kwargs["volumes"].keys())[0]

    assert first_vol == second_vol == VOL
    assert client.volumes.create.call_count == 1
    assert client.containers.run.call_count == 2


def test_volume_label_uses_hash_not_email():
    client = _make_client()
    ensure_user_container(EMAIL, client=client)
    labels = client.volumes.create.call_args.kwargs["labels"]
    values = list(labels.values())
    assert HASH in values
    assert EMAIL not in values
    for v in values:
        assert "@" not in v


# --- Group B: network hardening ----------------------------------------

def test_network_created_when_missing():
    client = _make_client()
    ensure_user_container(EMAIL, client=client)
    assert client.networks.create.call_count == 1
    kw = client.networks.create.call_args.kwargs
    assert kw["name"] == NET
    assert kw["driver"] == "bridge"
    assert kw["internal"] is False
    assert kw["labels"] == {NETWORK_LABEL_KEY: HASH}


def test_network_reused_when_present():
    client = _make_client(network_present=True)
    ensure_user_container(EMAIL, client=client)
    assert client.networks.create.call_count == 0


def test_network_label_uses_hash_not_email():
    client = _make_client()
    ensure_user_container(EMAIL, client=client)
    labels = client.networks.create.call_args.kwargs["labels"]
    values = list(labels.values())
    assert HASH in values
    assert EMAIL not in values
    for v in values:
        assert "@" not in v


def test_distinct_emails_get_distinct_networks():
    client = _make_client()
    ensure_user_container("alpha@example.com", client=client)
    ensure_user_container("beta@example.com", client=client)

    run_calls = client.containers.run.call_args_list
    assert len(run_calls) == 2
    net1 = run_calls[0].kwargs["network"]
    net2 = run_calls[1].kwargs["network"]
    assert net1 != net2

    create_calls = client.networks.create.call_args_list
    assert len(create_calls) == 2
    name1 = create_calls[0].kwargs["name"]
    name2 = create_calls[1].kwargs["name"]
    assert name1 != name2


def test_container_attached_only_to_own_network():
    client = _make_client()
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw["network"] == NET
    # No other network kwargs sneaking in.
    assert "networks" not in kw  # docker SDK uses singular `network`
    # And no other foreign network identifier in any kwarg value.
    for k, v in kw.items():
        if k == "network":
            continue
        assert NET not in repr(v) or k == "volumes"  # volumes mention vol/path, not net


# --- Group C: resource limits via env ----------------------------------

def test_mem_default_two_gigs_when_env_unset():
    client = _make_client()
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    # Use module constants (consistent with test_user_container.py) so this
    # test fails loudly if the defaults ever drift.
    assert kw["mem_limit"] == DEFAULT_MEM
    assert kw["nano_cpus"] == int(DEFAULT_CPUS * 1_000_000_000)


def test_mem_from_env(monkeypatch):
    monkeypatch.setenv(MEM_ENV, "4g")
    client = _make_client()
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw["mem_limit"] == "4g"


def test_cpus_from_env(monkeypatch):
    monkeypatch.setenv(CPUS_ENV, "2.5")
    client = _make_client()
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw["nano_cpus"] == 2_500_000_000


def test_cpus_env_invalid_raises(monkeypatch):
    monkeypatch.setenv(CPUS_ENV, "not-a-float")
    client = _make_client()
    with pytest.raises(ValueError) as excinfo:
        ensure_user_container(EMAIL, client=client)
    assert CPUS_ENV in str(excinfo.value)


# --- Group D: resource override JSON -----------------------------------

def test_override_file_mem_takes_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv(MEM_ENV, "4g")
    monkeypatch.setenv(OVERRIDES_DIR_ENV, str(tmp_path))
    (tmp_path / f"{HASH}.json").write_text(json.dumps({"mem": "8g"}))

    client = _make_client()
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw["mem_limit"] == "8g"
    # cpus not overridden → env default (unset) → built-in DEFAULT_CPUS.
    assert kw["nano_cpus"] == int(DEFAULT_CPUS * 1_000_000_000)


def test_override_file_cpus_takes_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv(OVERRIDES_DIR_ENV, str(tmp_path))
    (tmp_path / f"{HASH}.json").write_text(json.dumps({"cpus": 4}))

    client = _make_client()
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw["nano_cpus"] == 4_000_000_000
