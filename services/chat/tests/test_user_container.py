"""Unit tests for user_container.

All tests use a MagicMock docker client; no real daemon is touched.
"""

import json as _json
import os as _os
import re
from unittest.mock import MagicMock

import docker.errors
import pytest

import user_container
from user_container import (
    CLAUDE_RUNNER_CREDENTIALS_PATH,
    CLAUDE_RUNNER_GID,
    CLAUDE_RUNNER_HOME,
    CLAUDE_RUNNER_UID,
    DEFAULT_CPUS,
    DEFAULT_MEM,
    REFRESH_RATE_CAP_SECONDS,
    container_name_for,
    ensure_user_container,
    populate_credentials,
    refresh_credentials_if_stale,
)

# The un-patched resolver, captured at import so the resolve_usable_account
# tests can restore it (the module-wide ``exec_recorder`` fixture pins the
# function to identity for everything else).
_REAL_RESOLVE_USABLE_ACCOUNT = user_container.resolve_usable_account

# computed once; reused by all tests in this module
EMAIL = "test@example.com"
EXPECTED_NAME = container_name_for(EMAIL)
EXPECTED_VOLUME = f"{EXPECTED_NAME}-home"
EXPECTED_NETWORK = f"{EXPECTED_NAME}-net"



# ---------------------------------------------------------------------------
# Item 1 fixtures: mock the in-container exec seam and the host credentials
# file read so the existing ensure_user_container tests (which pass a
# MagicMock docker client) don't trip over the new post-up provisioning
# calls. New tests below opt back in by reading the recorder via the
# ``exec_recorder`` fixture.
# ---------------------------------------------------------------------------

CRED_BYTES = b'{"access_token":"fake","refresh_token":"r"}'


class _ExecRecorder:
    """Captures every (argv, stdin) tuple and serves a stat reply.

    `set_stat_reply` lets a test stub the in-container `stat -c %Y` output
    (the docker exec used by refresh_credentials_if_stale). Default reply
    is "missing" → returncode 1.
    """

    def __init__(self):
        self.calls = []  # list of {"argv": [...], "stdin": bytes|None}
        self._stat_returncode = 1
        self._stat_stdout = b""

    def set_stat_reply(self, *, mtime: float | None):
        if mtime is None:
            self._stat_returncode = 1
            self._stat_stdout = b""
        else:
            self._stat_returncode = 0
            self._stat_stdout = f"{mtime:.0f}\n".encode()

    def fake_run(self, argv, *, input=None, capture_output=True, check=True, timeout=None):
        self.calls.append({"argv": list(argv), "stdin": input})
        # If the command is `stat -c %Y <path>`, answer with our canned reply.
        is_stat = any(a == "stat" for a in argv) and "-c" in argv
        if is_stat:
            class _CP:
                pass
            cp = _CP()
            cp.returncode = self._stat_returncode
            cp.stdout = self._stat_stdout
            cp.stderr = b""
            return cp
        # Default: pretend the command succeeded.
        class _CP:
            pass
        cp = _CP()
        cp.returncode = 0
        cp.stdout = b""
        cp.stderr = b""
        return cp


@pytest.fixture
def exec_recorder(monkeypatch, tmp_path):
    rec = _ExecRecorder()
    # Patch user_container.subprocess.run — the module-local seam.
    monkeypatch.setattr(user_container.subprocess, "run", rec.fake_run)
    # Stage a fake host credentials file. populate_credentials calls
    # _host_credentials_path(account), which resolves the account's HOME via
    # ``account_router.home_for_account`` (the old CLAUDE_ACCOUNTS_HOST_PATH
    # constant is dead). Point that seam at a tmp layout we control, and pin
    # ``resolve_usable_account`` to identity so refresh_credentials_if_stale
    # never consults the router's usage probe for the requested account.
    import account_router

    fake_root = tmp_path / "claude-accounts"
    (fake_root / "account-1" / ".claude").mkdir(parents=True)
    host_creds = fake_root / "account-1" / ".claude" / ".credentials.json"
    host_creds.write_bytes(CRED_BYTES)
    monkeypatch.setattr(
        account_router, "home_for_account", lambda name: fake_root / (name or "main"),
    )
    monkeypatch.setattr(user_container, "resolve_usable_account", lambda requested: requested)
    rec.accounts_root = fake_root
    rec.host_creds_path = str(host_creds)
    # Item 3: redirect the per-user network allocations file at a tmp
    # path so the test suite never reads/writes /data/... on the host.
    alloc_path = tmp_path / "user-network-allocations.json"
    monkeypatch.setenv(user_container.USER_NETWORK_ALLOCATIONS_ENV, str(alloc_path))
    # Refresh cache is module-level; isolate per test.
    user_container._REFRESH_CACHE.clear()
    yield rec
    user_container._REFRESH_CACHE.clear()


@pytest.fixture(autouse=True)
def _autouse_exec_recorder(exec_recorder):
    """Apply exec_recorder to every test in this module so the existing
    suite (which never knew about the in-container exec seam) keeps
    passing without per-test edits."""
    return exec_recorder


def _make_client(
    container_present: bool = False,
    network_present: bool = True,
    volume_present: bool = True,
):
    """Build a MagicMock docker client with configurable get() side effects."""
    client = MagicMock()

    if container_present:
        client.containers.get.return_value = MagicMock(name="fake_container")
    else:
        client.containers.get.side_effect = docker.errors.NotFound("missing")

    if network_present:
        client.networks.get.return_value = MagicMock(name="fake_network")
    else:
        client.networks.get.side_effect = docker.errors.NotFound("missing")

    if volume_present:
        client.volumes.get.return_value = MagicMock(name="fake_volume")
    else:
        client.volumes.get.side_effect = docker.errors.NotFound("missing")

    return client


def _run_kwargs(client):
    return client.containers.run.call_args.kwargs


# --- name tests ---------------------------------------------------------

def test_container_name_format():
    assert re.match(r"^portfolio-user-[0-9a-f]{12}$", container_name_for("a@b.com"))


def test_container_name_deterministic():
    assert container_name_for("a@b.com") == container_name_for("a@b.com")


def test_container_name_distinct_for_distinct_emails():
    assert container_name_for("a@b.com") != container_name_for("c@d.com")


def test_container_name_case_normalized():
    assert container_name_for("Alice@example.com") == container_name_for("alice@example.com")
    assert container_name_for("  alice@example.com  ") == container_name_for("alice@example.com")


# --- ensure_user_container behavior -------------------------------------

def test_ensure_creates_when_missing():
    client = _make_client(container_present=False)
    result = ensure_user_container(EMAIL, client=client)
    assert result == EXPECTED_NAME
    assert client.containers.run.call_count == 1


def test_ensure_finds_when_present():
    client = _make_client(container_present=True)
    result = ensure_user_container(EMAIL, client=client)
    assert result == EXPECTED_NAME
    assert client.containers.run.call_count == 0


def test_ensure_idempotent_two_calls():
    client = MagicMock()
    client.networks.get.return_value = MagicMock()
    client.volumes.get.return_value = MagicMock()
    # First call: container missing. Second call: container present.
    client.containers.get.side_effect = [
        docker.errors.NotFound("missing"),
        MagicMock(name="fake_container"),
    ]

    n1 = ensure_user_container(EMAIL, client=client)
    n2 = ensure_user_container(EMAIL, client=client)
    assert n1 == n2 == EXPECTED_NAME
    assert client.containers.run.call_count == 1
    assert client.containers.get.call_count == 2


# --- run kwargs assertions ----------------------------------------------

def test_run_kwargs_image_and_user():
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw["image"] == "portfolio-tool/chat:dev"
    assert kw["user"] == "1000:1000"


def test_run_kwargs_command_and_restart():
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw["command"] == ["tail", "-f", "/dev/null"]
    assert kw["restart_policy"] == {"Name": "unless-stopped"}


def test_run_kwargs_resource_limits():
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    # Derive expected values from the module's own constants so this test
    # fails loudly if the defaults ever drift.
    assert kw["mem_limit"] == DEFAULT_MEM
    assert kw["nano_cpus"] == int(DEFAULT_CPUS * 1_000_000_000)


def test_run_kwargs_environment():
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    env = kw["environment"]
    assert env["HOME"] == "/workspace"
    # nix profiles (per-user /workspace, then the image's /app) come first so
    # nix-installed tools win; linuxbrew is still on the PATH after them.
    assert env["PATH"].startswith("/workspace/.nix-profile/bin:/app/.nix-profile/bin:")
    assert "/home/linuxbrew/.linuxbrew/bin" in env["PATH"].split(":")


def test_run_kwargs_mounts():
    """Item 1: per-user container mounts only the named volume + linuxbrew.
    The host /opt/wizerith/claude-accounts tree must NOT appear here —
    credentials are streamed in via populate_credentials instead, so uid
    1000 inside the container has no path to read them."""
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    volumes = kw["volumes"]
    assert volumes[EXPECTED_VOLUME] == {"bind": "/workspace", "mode": "rw"}
    # Inverted from the pre-Item-1 assertion: the host claude-accounts dir
    # must NOT be bind-mounted into the per-user container.
    assert "/opt/wizerith/claude-accounts" not in volumes
    for spec in volumes.values():
        bind = spec.get("bind", "") if isinstance(spec, dict) else ""
        assert "claude-accounts" not in bind


def test_run_kwargs_linuxbrew_mounted_readonly():
    """Per-user container needs /home/linuxbrew so `claude` (declared on PATH)
    actually resolves; mount must stay read-only — guests must not mutate the
    shared host install."""
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw["volumes"]["/home/linuxbrew"] == {
        "bind": "/home/linuxbrew",
        "mode": "ro",
    }


def test_run_kwargs_network():
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw["network"] == f"{EXPECTED_NAME}-net"


def test_run_kwargs_no_host_namespaces():
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw.get("pid_mode") != "host"
    assert kw.get("ipc_mode") != "host"


def test_run_kwargs_no_docker_sock():
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    for key, val in kw["volumes"].items():
        assert "/var/run/docker.sock" not in key
        bind = val.get("bind", "") if isinstance(val, dict) else ""
        assert "/var/run/docker.sock" not in bind


def test_run_kwargs_no_host_home():
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    for key, val in kw["volumes"].items():
        assert "/home/felix" not in key
        bind = val.get("bind", "") if isinstance(val, dict) else ""
        assert "/home/felix" not in bind


def test_run_kwargs_not_privileged():
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw.get("privileged", False) is not True


# --- network + volume provisioning -------------------------------------

def test_per_user_network_created_if_missing():
    client = _make_client(container_present=False, network_present=False)
    ensure_user_container(EMAIL, client=client)
    assert client.networks.create.call_count == 1
    create_kwargs = client.networks.create.call_args.kwargs
    assert create_kwargs.get("name") == f"{EXPECTED_NAME}-net"


def test_per_user_network_reused_if_present():
    client = _make_client(container_present=False, network_present=True)
    ensure_user_container(EMAIL, client=client)
    assert client.networks.create.call_count == 0


def test_per_user_volume_created_if_missing():
    client = _make_client(container_present=False, volume_present=False)
    ensure_user_container(EMAIL, client=client)
    assert client.volumes.create.call_count == 1
    create_kwargs = client.volumes.create.call_args.kwargs
    assert create_kwargs.get("name") == f"{EXPECTED_NAME}-home"


# --- [item 3] capability hardening assertions ---------------------------

def test_run_kwargs_cap_drop_all():
    """Per-user container drops ALL Linux capabilities by default."""
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw["cap_drop"] == ["ALL"]


def test_run_kwargs_cap_add_minimum_set():
    """Only the minimal cap_add list needed for claude/node to run as the
    unprivileged in-container user is re-added after the ALL drop."""
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw["cap_add"] == [
        "CHOWN",
        "SETUID",
        "SETGID",
        "DAC_OVERRIDE",
        "FOWNER",
        "KILL",
    ]


def test_run_kwargs_security_opt_no_new_privs():
    """no-new-privileges blocks setuid escalation paths post-drop."""
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    assert kw["security_opt"] == ["no-new-privileges:true"]


# ===========================================================================
# Item 1: post-up provisioning (workspace sharing, runner dirs, creds seed).
# ===========================================================================


def _exec_argvs(rec):
    """Just the argv lists, not the (argv, stdin) dicts."""
    return [c["argv"] for c in rec.calls]


def _find_call_with(rec, *needles):
    """Return the first recorded call whose argv contains every needle, or None."""
    for c in rec.calls:
        joined = " ".join(c["argv"])
        if all(n in joined for n in needles):
            return c
    return None


def test_provisioning_chowns_workspace_to_app_claude_runner_2775(exec_recorder):
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    call = _find_call_with(exec_recorder, "chown", "app:claude-runner", "/workspace")
    assert call is not None, (
        f"expected a chown of /workspace to app:claude-runner; "
        f"got argvs: {_exec_argvs(exec_recorder)}"
    )
    # Same shell snippet must also chmod 2775.
    assert "2775" in " ".join(call["argv"])
    # And it must run as root (only root can chown across users).
    assert call["argv"][:4] == ["docker", "exec", "--user", "root"]


def test_provisioning_creates_runner_home_0700_owner_2000(exec_recorder):
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    call = _find_call_with(exec_recorder, "mkdir", CLAUDE_RUNNER_HOME)
    assert call is not None
    joined = " ".join(call["argv"])
    assert f"chown -R {CLAUDE_RUNNER_UID}:{CLAUDE_RUNNER_GID}" in joined
    assert "chmod 0700" in joined
    assert call["argv"][:4] == ["docker", "exec", "--user", "root"]


def test_provisioning_seeds_credentials_mode_0400_owner_2000(exec_recorder):
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    call = _find_call_with(
        exec_recorder, "cat >", CLAUDE_RUNNER_CREDENTIALS_PATH, "chmod 0400"
    )
    assert call is not None, (
        "expected a populate_credentials shell snippet that writes the file, "
        "chowns 2000:2000, and chmods 0400"
    )
    joined = " ".join(call["argv"])
    assert f"chown {CLAUDE_RUNNER_UID}:{CLAUDE_RUNNER_GID}" in joined
    # Source bytes flow through stdin.
    assert call["stdin"] == CRED_BYTES
    # Must run as root (only root can chown the file to 2000:2000) and
    # attach stdin (`-i`) so the source bytes stream into `cat`.
    assert call["argv"][:5] == ["docker", "exec", "-i", "--user", "root"]


def test_per_user_container_volumes_omit_claude_accounts(exec_recorder):
    """Acceptance: the volume/bind spec produced by the provisioning code
    contains no reference to /opt/wizerith/claude-accounts."""
    client = _make_client(container_present=False)
    ensure_user_container(EMAIL, client=client)
    kw = _run_kwargs(client)
    volumes = kw["volumes"]
    for key, spec in volumes.items():
        assert "claude-accounts" not in key
        bind = spec.get("bind", "") if isinstance(spec, dict) else ""
        assert "claude-accounts" not in bind


# ===========================================================================
# Item 1: populate_credentials idempotency.
# ===========================================================================


def test_populate_credentials_idempotent(exec_recorder):
    populate_credentials("portfolio-user-aaa", "account-1")
    populate_credentials("portfolio-user-aaa", "account-1")
    write_calls = [
        c for c in exec_recorder.calls
        if c["stdin"] == CRED_BYTES
    ]
    assert len(write_calls) == 2
    # Both calls produced the same shell snippet (so the final file state
    # converges to mode 0400 owner 2000:2000 with the source bytes).
    assert write_calls[0]["argv"] == write_calls[1]["argv"]
    joined = " ".join(write_calls[0]["argv"])
    assert "chmod 0400" in joined
    assert f"chown {CLAUDE_RUNNER_UID}:{CLAUDE_RUNNER_GID}" in joined
    assert CLAUDE_RUNNER_CREDENTIALS_PATH in joined


# ===========================================================================
# Item 1: refresh_credentials_if_stale rate cap + stale-detection.
# ===========================================================================


def test_refresh_rate_cap_within_60s(exec_recorder, monkeypatch):
    """Two calls inside the rate-cap window: the second must NOT call
    populate_credentials (no second cred-streaming exec)."""
    # First call: stat says missing → populate runs.
    exec_recorder.set_stat_reply(mtime=None)
    refresh_credentials_if_stale("portfolio-user-rate", "account-1")
    first_writes = [c for c in exec_recorder.calls if c["stdin"] == CRED_BYTES]
    assert len(first_writes) == 1

    # Second call inside 60s: must short-circuit BEFORE issuing any docker
    # exec at all (no stat, no write).
    calls_before = len(exec_recorder.calls)
    refresh_credentials_if_stale("portfolio-user-rate", "account-1")
    assert len(exec_recorder.calls) == calls_before
    second_writes = [c for c in exec_recorder.calls if c["stdin"] == CRED_BYTES]
    assert len(second_writes) == 1  # unchanged


def test_refresh_triggers_when_in_container_file_missing(exec_recorder):
    exec_recorder.set_stat_reply(mtime=None)
    refresh_credentials_if_stale("portfolio-user-missing", "account-1")
    writes = [c for c in exec_recorder.calls if c["stdin"] == CRED_BYTES]
    assert len(writes) == 1


def test_refresh_triggers_when_in_container_file_older_than_1h(exec_recorder):
    import time as _time
    # File is 2h old.
    old = _time.time() - 7200
    exec_recorder.set_stat_reply(mtime=old)
    refresh_credentials_if_stale("portfolio-user-old", "account-1")
    writes = [c for c in exec_recorder.calls if c["stdin"] == CRED_BYTES]
    assert len(writes) == 1


def test_refresh_triggers_when_host_source_newer(exec_recorder, tmp_path, monkeypatch):
    import time as _time
    # In-container file is fresh (5 min old) — wouldn't trigger condition (b).
    fresh = _time.time() - 300
    exec_recorder.set_stat_reply(mtime=fresh)
    # But the host source mtime is newer than the in-container mtime.
    src_path = exec_recorder.host_creds_path
    newer = _time.time()  # right now > fresh (5 min ago)
    _os.utime(src_path, (newer, newer))
    refresh_credentials_if_stale("portfolio-user-newhost", "account-1")
    writes = [c for c in exec_recorder.calls if c["stdin"] == CRED_BYTES]
    assert len(writes) == 1


def test_refresh_no_op_when_in_container_fresh_and_host_older(exec_recorder):
    import time as _time
    # In-container file is 5 min old.
    in_container = _time.time() - 300
    exec_recorder.set_stat_reply(mtime=in_container)
    # Host source is OLDER than in-container (10 min old).
    src_path = exec_recorder.host_creds_path
    host_older = _time.time() - 600
    _os.utime(src_path, (host_older, host_older))
    refresh_credentials_if_stale("portfolio-user-fresh", "account-1")
    writes = [c for c in exec_recorder.calls if c["stdin"] == CRED_BYTES]
    assert len(writes) == 0


def test_refresh_rate_cap_uses_monotonic_clock(monkeypatch, exec_recorder):
    """Drive the rate cap explicitly: simulate the monotonic clock advancing
    past 60s between calls and confirm a second populate fires."""
    fake_now = [1000.0]

    def fake_monotonic():
        return fake_now[0]

    monkeypatch.setattr(user_container.time, "monotonic", fake_monotonic)
    exec_recorder.set_stat_reply(mtime=None)

    refresh_credentials_if_stale("portfolio-user-clock", "account-1")
    n1 = len([c for c in exec_recorder.calls if c["stdin"] == CRED_BYTES])
    assert n1 == 1

    # Advance past the 60s rate cap.
    fake_now[0] += REFRESH_RATE_CAP_SECONDS + 1
    refresh_credentials_if_stale("portfolio-user-clock", "account-1")
    n2 = len([c for c in exec_recorder.calls if c["stdin"] == CRED_BYTES])
    assert n2 == 2


# ===========================================================================
# Item 3: per-user network egress restrictions — subnet allocator + IPAM.
# ===========================================================================


def _alloc_path(monkeypatch, tmp_path, name="alloc.json"):
    """Point the allocator at a fresh tmp file; return its Path.

    Returning a Path (not str) lets the call sites use .read_text() /
    .write_text() / .exists() without re-wrapping.
    """
    p = tmp_path / name
    monkeypatch.setenv(user_container.USER_NETWORK_ALLOCATIONS_ENV, str(p))
    return p


def test_allocate_user_subnet_deterministic_per_email(monkeypatch, tmp_path):
    p = _alloc_path(monkeypatch, tmp_path)
    s1 = user_container.allocate_user_subnet("user@example.com")
    s2 = user_container.allocate_user_subnet("user@example.com")
    assert s1 == s2
    # Persisted on disk with the right schema shape.
    data = _json.loads(p.read_text())
    assert any(v == user_container._hash_for("user@example.com") for v in data.values())


def test_allocate_user_subnet_persists_across_simulated_restart(monkeypatch, tmp_path):
    p = _alloc_path(monkeypatch, tmp_path)
    s1 = user_container.allocate_user_subnet("alpha@example.com")
    # "Restart": clear in-process state. The allocator only has the file
    # for state, so re-importing isn't strictly required, but we exercise
    # the load path to be sure.
    s2 = user_container.allocate_user_subnet("alpha@example.com", allocations_path=p)
    assert s1 == s2


def test_allocate_user_subnet_distinct_emails_distinct_24s(monkeypatch, tmp_path):
    _alloc_path(monkeypatch, tmp_path)
    a = user_container.allocate_user_subnet("a@example.com")
    b = user_container.allocate_user_subnet("b@example.com")
    assert a != b
    # Both inside the pool /16.
    for s in (a, b):
        assert s.startswith("172.30.")
        assert s.endswith(".0/24")


def test_allocate_user_subnet_returns_pool_shaped_string(monkeypatch, tmp_path):
    _alloc_path(monkeypatch, tmp_path)
    s = user_container.allocate_user_subnet("shape@example.com")
    # "172.30.<n>.0/24" with 0 <= n < 256.
    assert s.startswith("172.30.")
    assert s.endswith(".0/24")
    n = int(s.split(".")[2])
    assert 0 <= n < 256


def test_allocate_user_subnet_collision_linear_probes_forward(monkeypatch, tmp_path):
    p = _alloc_path(monkeypatch, tmp_path)
    email = "probe@example.com"
    base = user_container._base_octet_for(email)
    # Pre-populate the base octet with a DIFFERENT email's hash, forcing
    # the linear probe to land on base+1 (mod 256).
    other_hash = "deadbeefcafe"
    p.write_text(_json.dumps({str(base): other_hash}))

    subnet = user_container.allocate_user_subnet(email)
    expected = (base + 1) % 256
    assert subnet == f"172.30.{expected}.0/24"

    # Persisted: the base octet still belongs to other_hash; our email
    # got base+1.
    persisted = _json.loads(p.read_text())
    assert persisted[str(base)] == other_hash
    assert persisted[str(expected)] == user_container._hash_for(email)


def test_allocate_user_subnet_probe_wraps_modulo_256(monkeypatch, tmp_path):
    """Pre-occupy octets [base..255] and confirm the probe wraps to 0+
    when needed, finding the first free slot from there."""
    p = _alloc_path(monkeypatch, tmp_path)
    email = "wrap@example.com"
    base = user_container._base_octet_for(email)
    occupied = {str(o): f"hash{o:03d}aaaa" for o in range(base, 256)}
    p.write_text(_json.dumps(occupied))

    subnet = user_container.allocate_user_subnet(email)
    # First free octet starting from base, mod 256, is 0 (since base..255
    # are taken and 0..base-1 are free).
    expected = 0
    assert subnet == f"172.30.{expected}.0/24"
    persisted = _json.loads(p.read_text())
    assert persisted["0"] == user_container._hash_for(email)


def test_allocate_user_subnet_pool_exhausted_raises(monkeypatch, tmp_path):
    p = _alloc_path(monkeypatch, tmp_path)
    occupied = {str(o): f"hash{o:03d}aaaa" for o in range(256)}
    p.write_text(_json.dumps(occupied))
    with pytest.raises(RuntimeError) as excinfo:
        user_container.allocate_user_subnet("late@example.com")
    msg = str(excinfo.value).lower()
    assert "pool" in msg or "exhausted" in msg or "256" in msg


def test_allocate_user_subnet_atomic_persistence_yields_valid_json(
    monkeypatch, tmp_path
):
    p = _alloc_path(monkeypatch, tmp_path)
    user_container.allocate_user_subnet("atomic@example.com")
    # File exists and parses as JSON; no leftover .tmp file.
    assert p.exists()
    parsed = _json.loads(p.read_text())
    assert isinstance(parsed, dict)
    assert any(
        v == user_container._hash_for("atomic@example.com")
        for v in parsed.values()
    )
    # The same-directory tempfile pattern leaves no orphan .tmp files
    # behind on the success path.
    siblings = _os.listdir(tmp_path)
    assert not any(s.endswith(".tmp") for s in siblings), siblings


def test_allocate_user_subnet_missing_file_treated_as_empty(monkeypatch, tmp_path):
    """File path doesn't exist yet → behaves like {}."""
    p = tmp_path / "nope.json"
    monkeypatch.setenv(user_container.USER_NETWORK_ALLOCATIONS_ENV, str(p))
    s = user_container.allocate_user_subnet("first@example.com")
    assert s.startswith("172.30.")
    assert p.exists()


def test_allocate_user_subnet_empty_file_treated_as_empty(monkeypatch, tmp_path):
    p = _alloc_path(monkeypatch, tmp_path)
    p.write_text("")
    s = user_container.allocate_user_subnet("empty@example.com")
    assert s.startswith("172.30.")


def test_allocate_user_subnet_malformed_json_raises(monkeypatch, tmp_path):
    p = _alloc_path(monkeypatch, tmp_path)
    p.write_text("{not valid json")
    with pytest.raises(ValueError):
        user_container.allocate_user_subnet("err@example.com")


# ---------------------------------------------------------------------------
# Item 3: per-user network creation path — IPAM, idempotency, legacy.
# ---------------------------------------------------------------------------


def _make_network_with_subnet(subnet):
    """A MagicMock that quacks like a docker SDK Network with a given Subnet."""
    net = MagicMock()
    net.attrs = {"IPAM": {"Config": [{"Subnet": subnet}]}}
    return net


def test_get_existing_network_subnet_returns_pool_subnet(monkeypatch, tmp_path):
    client = MagicMock()
    client.networks.get.return_value = _make_network_with_subnet("172.30.42.0/24")
    out = user_container.get_existing_network_subnet(client, "portfolio-user-x")
    assert out == "172.30.42.0/24"


def test_get_existing_network_subnet_returns_none_when_missing(monkeypatch, tmp_path):
    client = MagicMock()
    client.networks.get.side_effect = docker.errors.NotFound("missing")
    assert user_container.get_existing_network_subnet(client, "x") is None


def test_get_existing_network_subnet_returns_none_when_attrs_unparseable(
    monkeypatch, tmp_path
):
    client = MagicMock()
    # MagicMock attrs that's NOT a dict (e.g. another MagicMock).
    net = MagicMock()
    # Force attrs to be a non-dict so the helper short-circuits to None.
    net.attrs = MagicMock()
    client.networks.get.return_value = net
    assert user_container.get_existing_network_subnet(client, "x") is None


def test_network_create_uses_ipam_subnet_from_pool(exec_recorder, monkeypatch, tmp_path):
    """When the network does NOT exist, networks.create is called with
    an IPAM kwarg pinning a /24 from 172.30.0.0/16."""
    client = _make_client(container_present=False, network_present=False)
    # Container also missing → ensure_user_container will create it, which
    # is what we want so the network-creation path fires.
    ensure_user_container(EMAIL, client=client)
    assert client.networks.create.call_count == 1
    kwargs = client.networks.create.call_args.kwargs
    # The new IPAM kwarg is present and shapes a 172.30.X.0/24.
    assert "ipam" in kwargs
    ipam = kwargs["ipam"]
    # docker.types.IPAMConfig is a subclass of dict; the underlying shape
    # we care about is the Config[].Subnet string.
    subnet = None
    if hasattr(ipam, "get"):
        config = ipam.get("Config") or []
        if config and isinstance(config, list):
            subnet = config[0].get("Subnet")
    assert subnet is not None
    assert subnet.startswith("172.30.")
    assert subnet.endswith(".0/24")


def test_network_reused_when_existing_subnet_in_pool(exec_recorder, monkeypatch, tmp_path):
    """Network already exists with a 172.30.x/24 subnet → no create call,
    and crucially no allocator call either (we'd see a write to the
    allocations file otherwise)."""
    client = _make_client(container_present=False, network_present=True)
    # Override networks.get to return a network with a pool subnet.
    client.networks.get.return_value = _make_network_with_subnet("172.30.7.0/24")
    ensure_user_container(EMAIL, client=client)
    assert client.networks.create.call_count == 0
    # Allocations file should NOT have been written (no allocator call).
    from pathlib import Path as _Path
    alloc_path = _Path(_os.environ[user_container.USER_NETWORK_ALLOCATIONS_ENV])
    if alloc_path.exists():
        # If something did write, the file at least must NOT contain this
        # email's hash.
        data = _json.loads(alloc_path.read_text() or "{}")
        assert user_container._hash_for(EMAIL) not in data.values()


def test_network_legacy_non_pool_subnet_not_mutated(
    exec_recorder, monkeypatch, tmp_path, caplog
):
    """Network exists with a legacy non-pool subnet (e.g. 172.20.0.0/16):
    the code must NOT delete, mutate, or recreate it. A warning is logged.
    """
    import logging
    client = _make_client(container_present=False, network_present=True)
    client.networks.get.return_value = _make_network_with_subnet("172.20.0.0/16")
    with caplog.at_level(logging.WARNING, logger="user_container"):
        ensure_user_container(EMAIL, client=client)
    # No create, no remove, no anything-else.
    assert client.networks.create.call_count == 0
    # If the docker SDK had a remove/delete on the network MagicMock, it
    # was never invoked.
    legacy_net = client.networks.get.return_value
    assert not legacy_net.remove.called
    # Warning logged with the container/legacy markers.
    msgs = " ".join(rec.message for rec in caplog.records)
    assert "legacy" in msgs.lower() or "non-pool" in msgs.lower()


def test_network_unknown_ipam_treated_as_legacy(exec_recorder, monkeypatch, tmp_path):
    """Network present but the inspect attrs don't expose a usable Subnet
    (MagicMock-style). Code path: get_existing_network_subnet returns
    None → re-check with .get() → still present → log + reuse. No create,
    no allocator call."""
    client = _make_client(container_present=False, network_present=True)
    # Default _make_client returns a MagicMock(name=...) for networks.get;
    # MagicMock.attrs is a MagicMock, which our helper treats as None.
    ensure_user_container(EMAIL, client=client)
    assert client.networks.create.call_count == 0


def test_network_distinct_emails_get_distinct_pool_subnets(exec_recorder, monkeypatch, tmp_path):
    client_a = _make_client(container_present=False, network_present=False)
    client_b = _make_client(container_present=False, network_present=False)
    ensure_user_container("alpha-net@example.com", client=client_a)
    ensure_user_container("beta-net@example.com", client=client_b)

    sa = client_a.networks.create.call_args.kwargs["ipam"]["Config"][0]["Subnet"]
    sb = client_b.networks.create.call_args.kwargs["ipam"]["Config"][0]["Subnet"]
    assert sa != sb
    for s in (sa, sb):
        assert s.startswith("172.30.")


# ---------------------------------------------------------------------------
# resolve_usable_account — dynamic account resolution (felix 2026-06-11:
# "all containers should pick usable accounts automatically")
# ---------------------------------------------------------------------------

class _FakeRouter:
    class NoAccountsAvailable(RuntimeError):
        pass

    def __init__(self, usable: dict, pick_name: str | None):
        self._usable = usable
        self._pick_name = pick_name

    def is_usable(self, name):
        return self._usable.get(name, False)

    def pick(self):
        if self._pick_name is None:
            raise self.NoAccountsAvailable("all saturated")
        m = MagicMock()
        m.name = self._pick_name
        return m


def _patch_router(monkeypatch, router):
    """Swap in a fake ``account_router`` AND restore the real
    ``resolve_usable_account`` (the autouse ``exec_recorder`` pins it to
    identity), so these tests exercise the real resolver against the fake."""
    import sys
    monkeypatch.setitem(sys.modules, "account_router", router)
    monkeypatch.setattr(
        user_container, "resolve_usable_account", _REAL_RESOLVE_USABLE_ACCOUNT,
    )


def test_resolve_keeps_usable_requested(monkeypatch):
    _patch_router(monkeypatch, _FakeRouter({"account-2": True}, "account-3"))
    assert user_container.resolve_usable_account("account-2") == "account-2"


def test_resolve_switches_off_unusable_requested(monkeypatch):
    _patch_router(monkeypatch, _FakeRouter({"account-2": False}, "account-3"))
    assert user_container.resolve_usable_account("account-2") == "account-3"


def test_resolve_keeps_requested_when_pool_exhausted(monkeypatch):
    _patch_router(monkeypatch, _FakeRouter({"account-2": False}, None))
    assert user_container.resolve_usable_account("account-2") == "account-2"


def test_resolve_fails_static_on_probe_error(monkeypatch):
    router = _FakeRouter({}, "account-3")
    def boom(name):
        raise OSError("probe exploded")
    router.is_usable = boom
    _patch_router(monkeypatch, router)
    assert user_container.resolve_usable_account("account-2") == "account-2"


def test_refresh_streams_on_account_switch(monkeypatch):
    """When resolution lands on a different account than the container
    currently holds, the bearer is re-streamed even if mtimes look fresh."""
    _patch_router(monkeypatch, _FakeRouter({"account-2": False}, "account-3"))
    user_container._REFRESH_CACHE.clear()
    user_container._STREAMED_ACCOUNT.clear()
    user_container._STREAMED_ACCOUNT["c1"] = "account-2"

    monkeypatch.setattr(user_container, "_host_refresh_tokens_if_needed", lambda a: False)
    streamed = []
    monkeypatch.setattr(
        user_container, "populate_credentials",
        lambda c, a: streamed.append((c, a)),
    )
    # mtime helpers must not be reached — the switch short-circuits first.
    monkeypatch.setattr(
        user_container, "_in_container_mtime",
        lambda c: (_ for _ in ()).throw(AssertionError("mtime path reached")),
    )
    user_container.refresh_credentials_if_stale("c1", "account-2")
    assert streamed == [("c1", "account-3")]


def test_refresh_streams_on_switch_with_unknown_map_state(monkeypatch):
    """Post-restart (_STREAMED_ACCOUNT empty), a resolution SWITCH must
    still force-stream: the container holds the requested (unusable)
    account's bearer and would stay rate-limited until host rotation."""
    _patch_router(monkeypatch, _FakeRouter({"account-2": False}, "account-3"))
    user_container._REFRESH_CACHE.clear()
    user_container._STREAMED_ACCOUNT.clear()

    monkeypatch.setattr(user_container, "_host_refresh_tokens_if_needed", lambda a: False)
    streamed = []
    monkeypatch.setattr(
        user_container, "populate_credentials",
        lambda c, a: streamed.append((c, a)),
    )
    monkeypatch.setattr(
        user_container, "_in_container_mtime",
        lambda c: (_ for _ in ()).throw(AssertionError("mtime path reached")),
    )
    user_container.refresh_credentials_if_stale("c2", "account-2")
    assert streamed == [("c2", "account-3")]


def test_provisioning_survives_auth_proxy_failure(exec_recorder, monkeypatch):
    """The auth proxy only serves the claude-CLI path; with the pool dead it
    cannot start. That must not abort provisioning (2026-09-28: every new
    user got no container and all their turns failed closed)."""
    import user_container as uc

    def _boom(name):
        raise RuntimeError("auth proxy did not come up within 5s")

    monkeypatch.setattr(uc, "ensure_auth_proxy_running", _boom)
    client = _make_client(container_present=False)
    assert ensure_user_container(EMAIL, client=client) == EXPECTED_NAME
