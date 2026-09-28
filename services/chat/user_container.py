"""Per-user Docker container provisioner.

Phase 1 (multi-user-containers): foundation. Phase 3: persistence/isolation
hardening. Public signatures `container_name_for(email)` and
`ensure_user_container(email, client=None)` are FROZEN.

Item 1 (uid-separated execution) added the claude-runner identity (uid/gid
2000) and the credentials-seeding seam:

  * The per-user container no longer bind-mounts the host's
    /opt/wizerith/claude-accounts tree. Credentials are streamed in over
    `docker exec -i --user root` and land at
    /var/claude-runner/.claude/.credentials.json mode 0400 owner 2000:2000
    so uid 1000 (the user's interactive /workspace shell) cannot read them.
  * /workspace is shared between uid 1000 and gid 2000 via 2775 setgid so
    files claude-runner writes there inherit the claude-runner group and
    stay readable+writable by uid 1000.
  * `populate_credentials` / `refresh_credentials_if_stale` are the seam
    Phase N+1 (per-user container recreation) plugs into.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess  # SEAM: tests monkeypatch this for in-container exec calls.
import tempfile
import threading
import time
from typing import Optional

import docker
import docker.errors

USER_IMAGE = "portfolio-tool/chat:dev"
# 1g, not 2g. The host is a 7.6 GB box and five of these run concurrently
# (four per-user + shared); at 2g the sum of caps was 10 GB, so the caps
# never bound anything in practice. A claude session is ~300 MB resident,
# so 1g is still 3x headroom. See _resource_limits_for for the swap half
# of this fix, which is the part that actually caused the outage.
DEFAULT_MEM = "1g"
DEFAULT_CPUS = 1.0
WORKSPACE_PATH = "/workspace"

# Host path to the chat container's view of the per-account credential dirs.
# This module RUNS INSIDE the chat container, where the host directory is
# bind-mounted at the same path (see docker-compose chat service). The
# per-user containers do NOT mount this path; populate_credentials reads
# the file here and streams it into the per-user container.
CLAUDE_ACCOUNTS_HOST_PATH = "/opt/wizerith/claude-accounts"

# `claude`, `node`, and friends live under linuxbrew on the host. The chat
# container bind-mounts this read-only; per-user containers need the same so
# that `docker exec … claude …` (and an interactive shell via term-router)
# can actually find the binaries declared on PATH below.
LINUXBREW_HOST_PATH = "/home/linuxbrew"
LINUXBREW_CONTAINER_PATH = "/home/linuxbrew"
# Shared, read-only "Database" location surfaced in drive.wizerith.ai under
# Locations. Mounts the news engine's data volume (host /data/wpt-data, EBS
# nvme1n1) into every per-user + shared container at /workspace/database so
# all users can browse/download the parquet datasets and the news_export CSVs.
# Read-only: one user can never mutate the shared corpus for everyone. The
# Drive file API serves anything under /workspace, so no backend changes are
# needed. Env-overridable for parallel stacks; empty value disables the mount.
DATABASE_MOUNT_HOST_PATH = os.environ.get(
    "DATABASE_MOUNT_HOST_PATH", "/data/wpt-data"
)
DATABASE_MOUNT_CONTAINER_PATH = "/workspace/database"
# Per-user container/volume/network names use this prefix. Env-overridable so
# parallel stacks (e.g. wizerith.ai vs ald3.com) live in disjoint Docker
# namespaces and never collide on a recreate.
CONTAINER_NAME_PREFIX = os.environ.get(
    "USER_CONTAINER_PREFIX", "portfolio-user-"
)

# Shared workspace (Phase: shared-workspace).
#
# When SHARED_CONTAINER_NAME_ENV is set, callers can request a single
# long-lived "shared" container that all users on the tenant route into
# (instead of their per-email container). This is the wizerith.ai
# coworker-collaboration toggle: every chat or terminal session created
# with workspace="shared" lands in the same /workspace and shares files.
#
# The shared container reuses the entire per-user lifecycle (volume,
# bridge network with a /24 from the same pool, runner-dirs, workspace
# 2775 sharing, credential streaming + auth proxy), but its name and
# Claude account are FIXED at provision time rather than derived per-
# email. A fixed account avoids credential races: refresh_credentials_if_
# stale would otherwise pick a new account on every turn and racing
# refreshes from concurrent users would clobber the in-container bearer
# the auth proxy serves.
SHARED_CONTAINER_NAME_ENV = "WIZERITH_SHARED_CONTAINER_NAME"
SHARED_CONTAINER_ACCOUNT_ENV = "WIZERITH_SHARED_CONTAINER_ACCOUNT"
SHARED_CONTAINER_DEFAULT_ACCOUNT = "main"

# Item 1: claude-runner identity inside the per-user container.
CLAUDE_RUNNER_UID = 2000
CLAUDE_RUNNER_GID = 2000
CLAUDE_RUNNER_HOME = "/var/claude-runner"
CLAUDE_RUNNER_CLAUDE_DIR = "/var/claude-runner/.claude"
CLAUDE_RUNNER_CREDENTIALS_PATH = "/var/claude-runner/.claude/.credentials.json"

# /workspace is shared between the interactive uid 1000 user and the
# claude-runner group (gid 2000). 2775 = setgid + rwxrwxr-x so files
# created in /workspace inherit the claude-runner group, not the creator's
# primary group.
WORKSPACE_OWNER = "app:claude-runner"
WORKSPACE_MODE = "2775"

# Item N (auth-proxy / intra-container token isolation):
#
# claude is now dispatched as uid 1000 (NOT 2000) so the bash tool it
# spawns also runs as uid 1000 and cannot read CLAUDE_RUNNER_CREDENTIALS_PATH.
# The proxy below mediates Anthropic API calls: claude sends Authorization
# with a dummy bearer (read from WORKSPACE_DUMMY_CREDENTIALS_PATH), the proxy
# strips it and re-attaches the real bearer (read from
# CLAUDE_RUNNER_CREDENTIALS_PATH, only readable by uid 2000).
#
# AUTH_PROXY_PORT is loopback-only inside the per-user container; the
# per-user docker network has no host port mapping for it, so it is not
# reachable from the host or from sibling per-user containers.
AUTH_PROXY_PORT = 5557
AUTH_PROXY_LOG = "/var/claude-runner/auth_proxy.log"
AUTH_PROXY_PIDFILE = "/var/claude-runner/auth_proxy.pid"
WORKSPACE_CLAUDE_DIR = "/workspace/.claude"
WORKSPACE_DUMMY_CREDENTIALS_PATH = "/workspace/.claude/.credentials.json"
# Far-future expiresAt so claude never tries to OAuth-refresh the dummy
# token (refresh would hit /v1/oauth/token which the proxy returns 403 for).
# Year-2099 in milliseconds since epoch.
DUMMY_CREDENTIAL_EXPIRES_AT_MS = 4070908800000
DUMMY_ACCESS_TOKEN = "sk-ant-oat01-PROXY-MEDIATED-DUMMY-NOT-VALID-FOR-DIRECT-USE"
DUMMY_REFRESH_TOKEN = "sk-ant-ort01-PROXY-MEDIATED-DUMMY-NOT-VALID-FOR-DIRECT-USE"

# docker resource name suffixes — appended to the per-user container name
# to derive the matching named-volume and bridge-network names.
VOLUME_SUFFIX = "-home"
NETWORK_SUFFIX = "-net"

# Phase 3: labels and env-driven knobs.
VOLUME_LABEL_KEY = "portfolio-user"
NETWORK_LABEL_KEY = "portfolio-user"
NETWORK_DRIVER = "bridge"

# Env keys forwarded from the chat backend's process env into every
# per-user container's env at provision time. Set on the chat backend
# (docker-compose.yml `environment:` block); transparently propagated
# here. The forwarding is opt-in by key — we never blanket-forward
# os.environ because the chat backend has secrets (CF Access AUD,
# DB creds, Anthropic OAuth-related state) that MUST NOT reach a
# per-user container the user has shell access to.
#
# Each entry is forwarded only if non-empty in the chat backend env;
# absent / empty values just skip injection (so ald3 stays clean when
# only chat-wizerith sets FRED_API_KEY, etc.).
_USER_CONTAINER_FORWARDED_ENV_KEYS = (
    # Free FRED API key — unrestricted, intentionally not a secret.
    # Set on chat-wizerith only so wizerith.ai users get FRED data
    # out-of-the-box; ald3 sessions can still ask the user for one.
    "FRED_API_KEY",
)

# ---------------------------------------------------------------------------
# Item 3: per-user docker network egress restrictions.
#
# Every per-user bridge network is allocated a /24 carved from
# USER_NETWORK_POOL_CIDR (172.30.0.0/16). The DOCKER-USER iptables chain
# (installed by infra/setup/user-network-egress.sh) drops link-local +
# RFC1918 destinations from this pool, so per-user containers can reach
# the public internet (and the chat container's docker-socket-proxy on
# the configured docker-bridge gateway) but cannot reach 169.254/16,
# 10/8, other 172.16/12 ranges, or 192.168/16. Public egress is intact.
#
# Allocations are persisted to USER_NETWORK_ALLOCATIONS_PATH so a chat
# container restart re-emits the SAME /24 for the same email — without
# this, a restart followed by ensure_user_container would race a fresh
# allocation against the existing docker network's IPAM, surface as a
# "Pool overlaps" error, and require manual cleanup.
#
# Schema: { "<octet>": "<sha256(email)[:12]>" }. Octets are decimal
# string keys for JSON compatibility (json keys must be strings).
# ---------------------------------------------------------------------------
# The /16 reserved for per-user /24 networks. Env-overridable so parallel
# stacks each get a disjoint /16 (wizerith stack must NOT collide with ald3
# in docker IPAM). Format: "<a.b>." — three octets of a /16, trailing dot.
USER_NETWORK_POOL_PREFIX = os.environ.get(
    "USER_NETWORK_POOL_PREFIX", "172.30."
)
USER_NETWORK_POOL_CIDR = f"{USER_NETWORK_POOL_PREFIX}0.0/16"
USER_NETWORK_SUBNET_BITS = 24  # used by _format_pool_subnet below

# Security audit F-1 remediation (2026-05-28): the squid egress proxy is
# the single public-egress hole for the user pool. Its source ACL only
# accepts the user pool /16, and docker isolates separate bridges from one
# another — so the proxy must present ON each per-user/shared bridge, not
# on its own 172.20 home network. We connect it to every per-user net at a
# reserved high host (.250) that never collides with docker-assigned
# container IPs (allocated from .2 upward), and point HTTPS_PROXY at that
# per-net address. Container name + port are env-overridable to match the
# compose service.
EGRESS_PROXY_CONTAINER = os.environ.get(
    "WIZERITH_EGRESS_PROXY_CONTAINER", "wizerith-egress-proxy"
)
EGRESS_PROXY_PORT = int(os.environ.get("WIZERITH_EGRESS_PROXY_PORT", "3128"))
EGRESS_PROXY_HOST_OCTET = 250
USER_NETWORK_ALLOCATIONS_ENV = "PORTFOLIO_USER_NETWORK_ALLOCATIONS_PATH"
# Default allocations-file path. The original Phase 2 default was
# /data/user-network-allocations.json, but /data inside the chat image is
# root-owned (drwxr-xr-x) and the chat process runs as uid 1000, so
# write/makedirs both fail at first deploy attempt. We resolve to
# $HOME/.local/state/portfolio-tool/user-network-allocations.json
# instead — the chat container sets HOME=/home/felix and bind-mounts
# /home/felix:/home/felix:rw, so this is writable + persistent across
# container restarts without any docker-compose change. Operators who
# want a different location can still override with the env var above.
# Module-level lock guarding read-modify-write of the allocations file.
# Single-writer assumption: only the chat container provisions per-user
# networks; concurrent ensure_user_container calls within the chat
# process must be serialized, which this lock provides. Cross-process
# concurrency is NOT a target — the chat service is a single uvicorn.
_ALLOCATIONS_LOCK = threading.Lock()

MEM_ENV = "PORTFOLIO_USER_DEFAULT_MEM"
CPUS_ENV = "PORTFOLIO_USER_DEFAULT_CPUS"
PIDS_ENV = "PORTFOLIO_USER_DEFAULT_PIDS"
OVERRIDES_DIR_ENV = "PORTFOLIO_USER_OVERRIDES_DIR"
DEFAULT_OVERRIDES_DIR = "users"
OVERRIDE_KEY_MEM = "mem"
OVERRIDE_KEY_CPUS = "cpus"
OVERRIDE_KEY_PIDS = "pids"
# Defensive cap on processes per per-user container. The classic fork-bomb
# (`:(){ :|:& };:`) from the dev terminal or a runaway multiprocessing
# script can otherwise pin all host CPUs and exhaust PID namespace
# entries. 256 is plenty for normal research work (matplotlib + a venv +
# a few subprocesses) and far below the host's default 4096.
DEFAULT_PIDS = "256"

# Linuxbrew tools must precede system bins inside the user container so that
# `claude`, `node`, etc. installed under /home/linuxbrew shadow any
# distro-provided binaries on PATH. Per-user nix profile (under /workspace
# at runtime) and the seed nix profile (under /app, baked into the image)
# are prepended so non-interactive shells (e.g. `docker exec`-ed commands
# from the file UI) see nix-installed packages without sourcing
# /etc/profile. Nix 2.34+ uses XDG state paths; /app/.nix-profile is a
# stable symlink into /app/.local/state/nix/profiles/profile created by
# the installer.
_LINUXBREW_PATH = (
    "/workspace/.nix-profile/bin:"
    "/app/.nix-profile/bin:"
    "/home/linuxbrew/.linuxbrew/bin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)

# Item 1: in-process rate cap for refresh_credentials_if_stale. Maps
# container_name -> monotonic timestamp of last refresh attempt. 60s
# rate cap means at most one credential check per minute per container,
# regardless of how many turns the user fires.
_REFRESH_CACHE: dict[str, float] = {}
REFRESH_RATE_CAP_SECONDS = 60.0
# In-container file is considered stale if it's older than this. The chat
# host rotates credentials every few hours; 1h gives the rotation a chance
# to propagate without thrashing.
REFRESH_MAX_AGE_SECONDS = 3600.0

# OAuth access tokens with less than this much wall-clock life remaining
# count as "needs refresh". The earlier mtime-only check is unsafe because
# a freshly-copied file can still hold a long-expired token: the host
# systemd timer (`claude-token-refresh.timer`) only runs every few hours
# and there's a documented dead window where the host file's
# claudeAiOauth.expiresAt is in the past but no refresh is yet scheduled.
# Detecting that here and invoking the host refresh script lets per-user
# containers self-heal on the next turn instead of returning 401 until
# the wall-clock timer fires.
TOKEN_EXPIRY_GRACE_SECONDS = 10 * 60

# Path to the host-side refresh script. The chat container bind-mounts
# /home/felix read-write (see compose) so this binary is reachable. If
# absent we fall back to "wait for the systemd timer" — same behavior
# the codebase had before this self-heal path existed.
HOST_TOKEN_REFRESH_SCRIPT = os.environ.get(
    "ANTHROPIC_HOST_REFRESH_SCRIPT",
    "/home/felix/.local/bin/refresh-claude-tokens",
)
# Hard cap so a wedged refresh script can't stall every turn behind the
# refresh_credentials_if_stale call.
HOST_TOKEN_REFRESH_TIMEOUT_S = 60.0

_log = logging.getLogger(__name__)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _hash_for(email: str) -> str:
    """12-char sha256 prefix of the normalized email."""
    return hashlib.sha256(_normalize_email(email).encode()).hexdigest()[:12]


def container_name_for(email: str) -> str:
    """Deterministic, case- and whitespace-insensitive container name."""
    return f"{CONTAINER_NAME_PREFIX}{_hash_for(email)}"


def _volume_name_for(email: str) -> str:
    return f"{container_name_for(email)}{VOLUME_SUFFIX}"


def _network_name_for(email: str) -> str:
    return f"{container_name_for(email)}{NETWORK_SUFFIX}"


def _resource_limits_for(email: str) -> dict:
    """Env defaults overlaid with optional <overrides_dir>/<hash>.json."""
    mem = os.environ.get(MEM_ENV, DEFAULT_MEM)
    cpus_raw = os.environ.get(CPUS_ENV, str(DEFAULT_CPUS))
    pids_raw = os.environ.get(PIDS_ENV, DEFAULT_PIDS)
    try:
        cpus = float(cpus_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{CPUS_ENV} must be a float; got {cpus_raw!r}"
        ) from exc
    try:
        pids = int(pids_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{PIDS_ENV} must be an int; got {pids_raw!r}"
        ) from exc

    overrides_dir = os.environ.get(OVERRIDES_DIR_ENV) or DEFAULT_OVERRIDES_DIR
    override_path = os.path.join(overrides_dir, f"{_hash_for(email)}.json")
    if os.path.exists(override_path):
        try:
            with open(override_path, "r", encoding="utf-8") as f:
                override = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(
                f"malformed override JSON at {override_path}: {exc}"
            ) from exc
        if not isinstance(override, dict):
            raise ValueError(
                f"override JSON at {override_path} must be a JSON object"
            )
        known_keys = {OVERRIDE_KEY_MEM, OVERRIDE_KEY_CPUS, OVERRIDE_KEY_PIDS}
        unknown = sorted(k for k in override if k not in known_keys)
        if unknown:
            _log.warning(
                "ignoring unknown override key(s) %s in %s",
                unknown,
                override_path,
            )
        if OVERRIDE_KEY_MEM in override:
            mem = override[OVERRIDE_KEY_MEM]
        if OVERRIDE_KEY_CPUS in override:
            try:
                cpus = float(override[OVERRIDE_KEY_CPUS])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"override 'cpus' must be a float in {override_path}"
                ) from exc
        if OVERRIDE_KEY_PIDS in override:
            try:
                pids = int(override[OVERRIDE_KEY_PIDS])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"override 'pids' must be an int in {override_path}"
                ) from exc

    return {
        "mem_limit": mem,
        # memswap_limit == mem_limit disables swap for the container.
        # Docker's default when memswap is unset is 2x mem_limit, i.e. every
        # container silently got an extra mem_limit worth of HOST swap on top
        # of its RAM cap. Five containers x (2g RAM + 2g swap) = 20 GB of
        # permitted allocation against 7.6 GB RAM + 8 GB swap. Each container
        # stayed under its own limit the whole time, so docker never OOM-killed
        # anything -- the host just thrashed itself to death instead
        # (2026-08-17, three times in one night). With swap off a runaway
        # session hits its own cgroup limit and one claude process dies, which
        # is a contained failure instead of a total outage. Do NOT remove.
        "memswap_limit": mem,
        "nano_cpus": int(cpus * 1_000_000_000),
        "pids_limit": pids,
    }


def _ensure_named_volume(client, name: str, *, label_value: str) -> str:
    """Idempotently create a named docker volume.

    Shared between the per-user path (volume name derived from the email
    hash) and the shared-container path (volume name derived from the
    literal container name). The label_value parameter is what gets
    written into the VOLUME_LABEL_KEY label so `docker volume ls
    --filter label=portfolio-user=<value>` keeps working for both.
    """
    try:
        client.volumes.get(name)
    except docker.errors.NotFound:
        client.volumes.create(
            name=name,
            labels={VOLUME_LABEL_KEY: label_value},
        )
    return name


def _ensure_volume(client, email: str) -> str:
    """Idempotent: create the per-user volume with a hash label if missing."""
    return _ensure_named_volume(
        client,
        _volume_name_for(email),
        label_value=_hash_for(email),
    )


# ---------------------------------------------------------------------------
# Item 3: subnet allocator + network IPAM helpers.
# ---------------------------------------------------------------------------


def _allocations_path() -> str:
    """Resolve the allocations JSON path: env var override -> default.

    Default lives under $HOME/.local/state/portfolio-tool/ so the chat
    container's uid 1000 can write+makedirs without any image- or
    compose-level prep. See USER_NETWORK_ALLOCATIONS_ENV comment block
    above for the path-choice rationale.
    """
    override = os.environ.get(USER_NETWORK_ALLOCATIONS_ENV)
    if override:
        return override
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(
        home, ".local", "state", "portfolio-tool", "user-network-allocations.json"
    )


def _format_pool_subnet(octet: int) -> str:
    """Render the canonical pool /24 string for an octet.

    Centralises the f-string so USER_NETWORK_SUBNET_BITS is the single
    source of truth for the subnet prefix length — handy if the pool
    is ever resized to a different /N split.
    """
    return f"{USER_NETWORK_POOL_PREFIX}{octet}.0/{USER_NETWORK_SUBNET_BITS}"


def _base_octet_for(email: str) -> int:
    """Deterministic starting octet for an email's linear probe.

    Reuses `_hash_for` (the same 12-char sha256 prefix used as the
    allocations-file value) so the two derivations stay in lockstep
    if the hash length ever changes. Mod 256 puts the same email at
    the same starting octet every call, which is what makes
    allocate_user_subnet idempotent on a fresh (or absent)
    allocations file across restarts.
    """
    return int(_hash_for(email), 16) % 256


def _load_allocations(allocations_path: str) -> dict:
    """Return parsed allocations file contents, or {} if missing/empty.

    Any other read or parse failure surfaces as ValueError so the caller
    can decide policy — silently overwriting a corrupted file would
    quietly forget every existing allocation and provoke "Pool overlaps"
    on the next docker network create.
    """
    if not os.path.exists(allocations_path):
        return {}
    try:
        with open(allocations_path, "r", encoding="utf-8") as fh:
            data = fh.read()
    except OSError as exc:
        raise ValueError(
            f"cannot read allocations file at {allocations_path}: {exc}"
        ) from exc
    if not data.strip():
        return {}
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"malformed allocations JSON at {allocations_path}: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"allocations file at {allocations_path} must contain a JSON object"
        )
    return parsed


def _atomic_write_json(target_path: str, payload: dict) -> None:
    """Write `payload` as JSON to `target_path` atomically.

    Same-directory tempfile + fsync + os.rename is the only crash-safe
    pattern on POSIX — a write that doesn't atomic-rename can leave the
    file truncated mid-write if the process dies, and a write to a
    different filesystem cannot be atomically renamed across mountpoints.
    """
    parent = os.path.dirname(target_path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".allocations.", suffix=".json.tmp", dir=parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp_path, target_path)
    except Exception:
        # Best-effort cleanup of the orphaned tempfile, then re-raise the
        # ORIGINAL error unchanged so the caller sees the real failure
        # (the cleanup itself is silent on its own OSError).
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def allocate_user_subnet(
    email: str, *, allocations_path: Optional[str] = None
) -> str:
    """Return a deterministic /24 from the 172.30.0.0/16 pool for `email`.

    Idempotent: an email that already has a persisted octet returns the
    same /24 across calls (and across chat container restarts). New emails
    start at sha256(email)[:12] % 256 and linear-probe forward (mod 256)
    until they find an unoccupied octet, which is then persisted via an
    atomic rename. Concurrent in-process calls are serialised by the
    module-level _ALLOCATIONS_LOCK; cross-process concurrency is out of
    scope (single-uvicorn assumption).

    Raises RuntimeError if all 256 octets in the pool are occupied. We
    cap at 256 because the pool is a /16 split into /24s — there are
    exactly 256 of them.
    """
    target_path = allocations_path or _allocations_path()
    email_hash = _hash_for(email)

    with _ALLOCATIONS_LOCK:
        allocations = _load_allocations(target_path)

        # Idempotent return for an email already in the table.
        for octet_str, hash_prefix in allocations.items():
            if hash_prefix == email_hash:
                return _format_pool_subnet(int(octet_str))

        base = _base_octet_for(email)
        # Linear probe forward, mod 256.
        for offset in range(256):
            candidate = (base + offset) % 256
            key = str(candidate)
            if key not in allocations:
                allocations[key] = email_hash
                _atomic_write_json(target_path, allocations)
                return _format_pool_subnet(candidate)

        raise RuntimeError(
            f"user network pool {USER_NETWORK_POOL_CIDR} exhausted: "
            f"all 256 /24 subnets are occupied; cannot allocate for {email_hash}"
        )


def get_existing_network_subnet(client, network_name: str) -> Optional[str]:
    """Return the IPAM Subnet of `network_name` if it exists, else None.

    Returns None for: network missing entirely, network present but with
    no IPAM config we can parse (MagicMock in tests, or a malformed
    inspect response). The caller treats None as "subnet unknown" and
    handles it the same as a legacy non-pool subnet — leave the network
    alone, don't try to mutate it.
    """
    try:
        network = client.networks.get(network_name)
    except docker.errors.NotFound:
        return None
    attrs = getattr(network, "attrs", None)
    if not isinstance(attrs, dict):
        return None
    ipam = attrs.get("IPAM")
    if not isinstance(ipam, dict):
        return None
    config = ipam.get("Config")
    if not isinstance(config, list):
        return None
    for entry in config:
        if isinstance(entry, dict):
            subnet = entry.get("Subnet")
            if isinstance(subnet, str) and subnet:
                return subnet
    return None


def _subnet_in_pool(subnet: Optional[str]) -> bool:
    """True iff `subnet` is a string starting with our pool's /16 prefix.

    A literal-prefix check (rather than ipaddress.ip_network containment)
    keeps the dependency surface zero and is sufficient because every
    subnet we ourselves allocate is shaped "172.30.<n>.0/24". Anything
    else — a 172.20/16, a 10/8, a 192.168/24, a hand-edited /23 inside
    our pool — falls through to "treat as legacy".
    """
    if not subnet:
        return False
    return subnet.startswith(USER_NETWORK_POOL_PREFIX)


def _ensure_named_network(
    client, name: str, *, key: str, label_value: str
) -> str:
    """Idempotently create a bridge network with a /24 from the user pool.

    `key` is the deterministic-allocation key passed to
    allocate_user_subnet — the per-user path uses the email; the shared
    path uses the container name. Same reconciliation rules as
    _ensure_network: pool-subnet networks are reused, legacy / non-pool
    / unparseable networks are left alone with a warning, missing
    networks are created with a fresh /24 from the pool.
    """
    existing_subnet = get_existing_network_subnet(client, name)

    if existing_subnet is not None:
        # Network exists. Decide reuse vs. legacy-warn.
        if _subnet_in_pool(existing_subnet):
            return name
        _log.warning(
            "legacy non-pool subnet for %s: %s; will be reconciled by "
            "container recreate phase",
            name, existing_subnet,
        )
        return name

    # Re-check to distinguish NotFound from present-with-no-parseable-IPAM
    # (get_existing_network_subnet returned None for both). NotFound -> we
    # own the create; present-but-no-IPAM -> treat as legacy, log + reuse.
    # Both are non-fatal.
    try:
        client.networks.get(name)
        # Came back present this time with no parseable subnet. Same
        # legacy treatment as above.
        _log.warning(
            "network %s exists with no parseable IPAM subnet; "
            "leaving as-is for the container recreate phase",
            name,
        )
        return name
    except docker.errors.NotFound:
        pass

    subnet = allocate_user_subnet(key)
    client.networks.create(
        name=name,
        driver=NETWORK_DRIVER,
        internal=False,
        labels={NETWORK_LABEL_KEY: label_value},
        ipam=docker.types.IPAMConfig(
            pool_configs=[docker.types.IPAMPool(subnet=subnet)],
        ),
    )
    return name


def _ensure_network(client, email: str) -> str:
    """Idempotent: ensure a per-user bridge network with the right subnet.

    Thin wrapper around _ensure_named_network — derives the network name
    from the email's hash (stable per-user) and uses the email itself as
    both the allocator key and the label value (legacy label format,
    preserved so existing inspect/audit tooling keeps working).
    """
    return _ensure_named_network(
        client,
        _network_name_for(email),
        key=email,
        label_value=_hash_for(email),
    )


def _egress_proxy_ip_for_subnet(subnet: str) -> Optional[str]:
    """Reserved squid IP on a per-user/shared /24, e.g. 172.31.120.250.

    Returns None for a subnet we can't parse — the caller then leaves the
    container without a working proxy rather than guessing an address.
    """
    if not subnet:
        return None
    net = subnet.split("/", 1)[0]
    parts = net.split(".")
    if len(parts) != 4:
        return None
    return f"{parts[0]}.{parts[1]}.{parts[2]}.{EGRESS_PROXY_HOST_OCTET}"


def _ensure_egress_proxy_attached(
    client, network_name: str, proxy_ip: str
) -> None:
    """Idempotently connect the squid egress proxy to a per-user/shared net.

    The F-1 lockdown DROPs all user-pool -> public traffic except to the
    proxy, and squid's source ACL only accepts the user pool /16 — so the
    proxy has to be reachable FROM the user bridge with a user-pool source
    IP. We attach it at the reserved high host so the user container reaches
    it on-subnet (no cross-bridge routing, which docker isolates anyway).

    Runs on every provision call so existing nets self-heal after a proxy
    recreate (compose `up -d wizerith-egress-proxy` drops manual attaches).
    """
    try:
        proxy = client.containers.get(EGRESS_PROXY_CONTAINER)
    except docker.errors.NotFound:
        _log.warning(
            "egress proxy %s not found; user container on %s will have no "
            "public egress until it is started and reprovisioned",
            EGRESS_PROXY_CONTAINER, network_name,
        )
        return

    networks = proxy.attrs.get("NetworkSettings", {}).get("Networks", {})
    if network_name in networks:
        return

    try:
        client.networks.get(network_name).connect(
            proxy, ipv4_address=proxy_ip
        )
    except docker.errors.APIError as exc:
        _log.warning(
            "could not attach egress proxy %s to %s at %s: %s",
            EGRESS_PROXY_CONTAINER, network_name, proxy_ip, exc,
        )


# ---------------------------------------------------------------------------
# Item 1: in-container provisioning helpers.
#
# All in-container side-effects go through `_docker_exec` so tests have a
# single subprocess seam to monkeypatch. We use the docker CLI (not the
# python-docker SDK) for these because the credential-streaming case needs
# stdin and the SDK's exec_run does not stream stdin cleanly.
# ---------------------------------------------------------------------------


def _docker_exec(
    container_name: str,
    cmd: list[str],
    *,
    user: str = "root",
    stdin_bytes: Optional[bytes] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run `docker exec [--user <u>] -i <container> <cmd...>` synchronously.

    Tests monkeypatch `subprocess.run` (or this whole function) to capture
    the argv + stdin payload. Exposed at module scope so the credential
    streaming path and the chown/chmod calls share one seam.
    """
    argv = ["docker", "exec"]
    if stdin_bytes is not None:
        argv.append("-i")
    argv.extend(["--user", user, container_name, *cmd])
    return subprocess.run(
        argv,
        input=stdin_bytes,
        capture_output=True,
        check=check,
    )


def _setup_runner_dirs(container_name: str) -> None:
    """Idempotently create /var/claude-runner{,/.claude} 0700 owner 2000:2000.

    Runs as root inside the per-user container so the chown can take effect.
    Safe to invoke on every ensure_user_container call: mkdir -p / chown -R /
    chmod are no-ops when the target already matches.
    """
    script = (
        "set -e; "
        f"mkdir -p {CLAUDE_RUNNER_HOME} {CLAUDE_RUNNER_CLAUDE_DIR}; "
        f"chown -R {CLAUDE_RUNNER_UID}:{CLAUDE_RUNNER_GID} {CLAUDE_RUNNER_HOME}; "
        f"chmod 0700 {CLAUDE_RUNNER_HOME} {CLAUDE_RUNNER_CLAUDE_DIR}"
    )
    _docker_exec(container_name, ["sh", "-c", script], user="root")


def _setup_workspace_sharing(container_name: str) -> None:
    """Chown /workspace to app:claude-runner mode 2775 (setgid).

    The setgid bit is what makes files written by uid 1000 inherit the
    claude-runner gid, so credentials staged here by claude (and anything
    the user drops into /workspace) stay group-readable for the runner.

    Per-user containers run with cap_drop=ALL minus a small whitelist that
    intentionally excludes CAP_FSETID. Without CAP_FSETID, chmod silently
    strips the setgid bit unless the calling process's effective gid
    matches the file's group (Linux chmod(2): "S_ISGID bit will be turned
    off, but this will not cause an error to be returned"). Root in the
    container has gid 0; the workspace is gid claude-runner (2000); so a
    naive `chmod 2775` from root produces 0775 silently. We wrap chmod in
    `sg claude-runner -c '...'` so it runs with egid=2000 matching the
    file's group, and the setgid bit sticks. Adding CAP_FSETID would also
    work but expands the per-user container's capability surface for one
    chmod; the sg wrapper keeps the cap_drop posture intact.
    """
    script = (
        "set -e; "
        f"chown {WORKSPACE_OWNER} {WORKSPACE_PATH}; "
        f"sg claude-runner -c 'chmod {WORKSPACE_MODE} {WORKSPACE_PATH}'"
    )
    _docker_exec(container_name, ["sh", "-c", script], user="root")


def _host_credentials_path(account: str) -> str:
    """Resolve the chat container's view of the source credentials file.

    Two account layouts coexist on the host and account_router can pick
    either:
      * ``main`` — bridge user's primary, at $HOME/.claude/.credentials.json
        (chat mounts /home/felix:rw, so this is reachable here too).
      * ``<wizerith name>`` — secondary accounts under
        /opt/wizerith/claude-accounts/<name>/.claude/.credentials.json.

    Pre-fix this function hard-coded the wizerith layout for *every*
    account, so when pick() chose ``main`` for a fresh user, populating
    credentials raised FileNotFoundError on a non-existent
    /opt/wizerith/claude-accounts/main/... path and the per-user
    container was left unprovisioned — surfacing in the chat UI as
    "this session has no provisioned per-user container".

    Delegate the per-name lookup to ``account_router.home_for_account``
    (single source of truth for where each account's credentials live).
    NOT ``list_accounts()`` — that applies the CHAT_ACCOUNT_EXCLUDE pick
    filter, which gates *new-session* routing only. Resolving an
    *existing* session's locked account must ignore the filter: routing
    sessions away from ``main`` for new picks must not strand the many
    sessions already locked to it (post-exclude, list_accounts dropped
    ``main`` → refresh_credentials_if_stale raised FileNotFoundError on
    the wizerith-convention fallback path → streamed tokens went stale →
    every turn 401'd).
    Same dual-context import pattern as ``_account_for_email``: relative
    when loaded as services.chat.user_container (term-router), bare when
    loaded flat (chat image). The per-user container does NOT see the
    resolved path either way.
    """
    try:
        from . import account_router
    except ImportError:
        import account_router
    home_path = account_router.home_for_account(account)
    return os.path.join(str(home_path), ".claude", ".credentials.json")


# Which account's bearer each container currently holds, by container name.
# Written by populate_credentials, read by refresh_credentials_if_stale to
# detect that dynamic account resolution switched a container's account
# (mtime comparisons can't see that — they compare against ONE host file).
# In-process only: after a chat restart the first refresh per container
# repopulates unconditionally, which is cheap and self-corrects the map.
_STREAMED_ACCOUNT: dict[str, str] = {}
# Serializes credential writes per container so two concurrent turns can't
# interleave `cat >` payloads into a corrupt file. Account *resolution* is
# deterministic within the usage cache TTL, so serialized writers converge
# on the same bearer rather than flapping.
_POPULATE_LOCKS: dict[str, threading.Lock] = {}
_POPULATE_LOCKS_GUARD = threading.Lock()


def _served_account(container_name: Optional[str], account: Optional[str]) -> Optional[str]:
    """The account whose bearer actually served this turn. ``resolve_usable_
    account`` may have swapped away from the session's stored preference, so the
    bearer streamed into the container (_STREAMED_ACCOUNT) is authoritative;
    fall back to the passed account for host/admin dispatch (no container)."""
    if container_name is not None:
        streamed = _STREAMED_ACCOUNT.get(container_name)
        if streamed:
            return streamed
    return account


def note_turn_rate_limited(
    container_name: Optional[str], account: Optional[str], resets_at: Optional[float],
) -> None:
    """Feed an OBSERVED 429 back to the router's cooldown so it stops routing
    into a saturated account. The usage dashboard lags the live limiter, so
    this dispatch-time signal is the only reliable saturation detector — see
    account_router.mark_hot. ``resets_at`` is the rate-limit event's wall-clock
    reset (epoch seconds) when the stream provided one, else None (mark_hot
    applies a default window). Best-effort: never raises into the turn."""
    name = _served_account(container_name, account)
    if not name:
        return
    try:
        from . import account_router
    except ImportError:
        import account_router
    try:
        account_router.mark_hot(name, resets_at)
    except Exception:
        _log.exception("note_turn_rate_limited: mark_hot failed for %s", name)


def note_turn_success(container_name: Optional[str], account: Optional[str]) -> None:
    """Clear any cooldown on the account that just served a successful turn —
    proves it recovered, so the router can use it again immediately rather than
    waiting out a possibly-conservative deadline. Best-effort."""
    name = _served_account(container_name, account)
    if not name:
        return
    try:
        from . import account_router
    except ImportError:
        import account_router
    try:
        account_router.note_success(name)
    except Exception:
        _log.exception("note_turn_success: note_success failed for %s", name)


def _populate_lock(container_name: str) -> threading.Lock:
    with _POPULATE_LOCKS_GUARD:
        return _POPULATE_LOCKS.setdefault(container_name, threading.Lock())


def populate_credentials(container_name: str, account: str) -> None:
    """Stream the host's per-account credentials file into the per-user
    container at /var/claude-runner/.claude/.credentials.json.

    Idempotent: repeated calls leave a single file with mode 0400 owner
    2000:2000 and content matching source. We use `docker exec -i --user
    root` (not `docker cp`, which does not set ownership) and chown +
    chmod in the same shell snippet so partial-state windows are
    impossible — either the file lands with the right perms or the exec
    fails and we surface the error.
    """
    src = _host_credentials_path(account)
    with open(src, "rb") as fh:
        payload = fh.read()
    script = (
        f"set -e; "
        f"cat > {CLAUDE_RUNNER_CREDENTIALS_PATH} && "
        f"chown {CLAUDE_RUNNER_UID}:{CLAUDE_RUNNER_GID} "
        f"{CLAUDE_RUNNER_CREDENTIALS_PATH} && "
        f"chmod 0400 {CLAUDE_RUNNER_CREDENTIALS_PATH}"
    )
    with _populate_lock(container_name):
        _docker_exec(
            container_name,
            ["sh", "-c", script],
            user="root",
            stdin_bytes=payload,
        )
        _STREAMED_ACCOUNT[container_name] = account


def resolve_usable_account(requested: str) -> str:
    """Resolve a session's/env's preferred account to one that is USABLE.

    Containers must end up on an account that can actually serve turns
    (felix, 2026-06-11: "all containers should pick usable accounts
    automatically") — static pinning left the shared workspace wedged on
    a saturated account while a fresh one sat idle. The preference is
    honored while alive-with-headroom, transparently swapped for the
    router's best pick while saturated or dead, and naturally returned
    to once healthy (every resolution re-prefers it). Conversation
    continuity survives the swap: session history lives in the container;
    the streamed bearer only decides which plan gets billed.

    Fails static: any probe/pick error keeps the requested account, which
    is exactly the pre-resolution behavior.
    """
    try:
        from . import account_router
    except ImportError:
        import account_router
    try:
        if account_router.is_usable(requested):
            return requested
    except Exception:
        _log.exception("usability probe failed for %s; keeping it", requested)
        return requested
    try:
        choice = account_router.pick().name
    except account_router.NoAccountsAvailable:
        _log.warning(
            "account %s is unusable (saturated or dead token) and the pool "
            "has no usable alternative; keeping it", requested,
        )
        return requested
    except Exception:
        _log.exception("router pick failed; keeping account %s", requested)
        return requested
    if choice != requested:
        _log.warning(
            "account %s is unusable (saturated or dead token); routing "
            "container turns to %s until it recovers", requested, choice,
        )
    return choice


def _write_workspace_dummy_credentials(
    container_name: str, account: Optional[str]
) -> None:
    """Stage a DUMMY claudeAiOauth file at WORKSPACE_DUMMY_CREDENTIALS_PATH.

    claude (dispatched as uid 1000) reads $HOME/.claude/.credentials.json
    to learn the bearer it should send as Authorization. With the auth
    proxy in place, the value of that bearer is irrelevant — the proxy
    strips claude's Authorization and re-attaches the real one server-
    side. But claude still requires a well-shaped credentials file to
    enter OAuth mode rather than failing with "no credentials found".

    Non-secret fields (scopes, subscriptionType, rateLimitTier) are
    copied from the real credential where present so claude's local
    feature detection (e.g., scope-gated tools, plan-tier UX) keeps
    matching the real account. expiresAt is pinned to year 2099 so
    claude never tries to refresh — refresh would hit /v1/oauth/token
    which the proxy 403s, and we don't want claude to blow up the turn
    on a refresh failure when the real token is still valid.
    """
    real_oauth: dict = {}
    if account is not None:
        try:
            with open(
                _host_credentials_path(account), "r", encoding="utf-8"
            ) as fh:
                real_oauth = json.load(fh).get("claudeAiOauth", {}) or {}
        except Exception:
            # Real cred unreadable/missing, or the account name doesn't
            # resolve — proceed with conservative defaults. The proxy still
            # mediates auth, so the only consequence is local feature
            # detection inside claude. (account is None when no usable
            # account exists at provision time; the dummy is still staged so
            # a previously self-wiped credential can't persist as a sticky
            # "Not logged in".)
            pass

    dummy = {
        "claudeAiOauth": {
            "accessToken": DUMMY_ACCESS_TOKEN,
            "refreshToken": DUMMY_REFRESH_TOKEN,
            "expiresAt": DUMMY_CREDENTIAL_EXPIRES_AT_MS,
            "scopes": real_oauth.get("scopes") or [
                "user:file_upload",
                "user:inference",
                "user:mcp_servers",
                "user:profile",
                "user:sessions:claude_code",
            ],
            "subscriptionType": real_oauth.get("subscriptionType") or "max",
            "rateLimitTier": real_oauth.get("rateLimitTier") or "default",
        }
    }
    payload = json.dumps(dummy, indent=2).encode("utf-8")
    script = (
        "set -e; "
        f"mkdir -p {WORKSPACE_CLAUDE_DIR}; "
        f"cat > {WORKSPACE_DUMMY_CREDENTIALS_PATH} && "
        f"chown 1000:1000 {WORKSPACE_CLAUDE_DIR} "
        f"{WORKSPACE_DUMMY_CREDENTIALS_PATH} && "
        f"chmod 0644 {WORKSPACE_DUMMY_CREDENTIALS_PATH}"
    )
    _docker_exec(
        container_name,
        ["sh", "-c", script],
        user="root",
        stdin_bytes=payload,
    )


def _is_auth_proxy_running(container_name: str) -> bool:
    """True iff the in-container auth proxy responds 200 on its health endpoint.

    Uses the per-user container's bundled python3 (chat:dev image) to
    probe http://127.0.0.1:AUTH_PROXY_PORT/__proxy_health — no extra
    binary dependency on the per-user container. A 2-second timeout
    covers cold-start + slow-loop scenarios without blocking the chat
    request thread.
    """
    py_probe = (
        "import sys, urllib.request; "
        f"r = urllib.request.urlopen('http://127.0.0.1:{AUTH_PROXY_PORT}"
        "/__proxy_health', timeout=2); "
        "sys.exit(0 if r.status == 200 else 1)"
    )
    proc = _docker_exec(
        container_name,
        ["python3", "-c", py_probe],
        user="root",
        check=False,
    )
    return proc.returncode == 0


def start_auth_proxy(container_name: str) -> None:
    """Spawn the auth proxy as uid 2000 inside the per-user container.

    `docker exec -d` detaches from the chat backend's stdio. `setsid`
    puts the proxy in its own session so signals to the chat backend
    never propagate here. stdout+stderr go to AUTH_PROXY_LOG owned by
    uid 2000 — the uid 1000 shell can neither read nor tail it.

    Not idempotent on its own: if a proxy is already bound to
    AUTH_PROXY_PORT the second instance will exit on bind failure but
    the call still returns success. Callers should use
    ensure_auth_proxy_running, which probes first.
    """
    inner = (
        f"setsid /usr/local/bin/python3 /app/anthropic_auth_proxy.py "
        f">> {AUTH_PROXY_LOG} 2>&1 &"
    )
    argv = [
        "docker", "exec", "-d",
        "--user", f"{CLAUDE_RUNNER_UID}:{CLAUDE_RUNNER_GID}",
        "-w", CLAUDE_RUNNER_HOME,
        container_name,
        "sh", "-c", inner,
    ]
    subprocess.run(argv, capture_output=True, check=True)


def ensure_auth_proxy_running(container_name: str) -> None:
    """Idempotent: probe the proxy, start it if absent, wait for bind.

    Called from both ensure_user_container (initial provision) and
    refresh_credentials_if_stale (every turn) so a per-user container
    restart self-heals on the next request rather than ECONNREFUSE-ing
    the user. Bounded 5-second poll so a misconfigured proxy surfaces
    as a runner error instead of a hung request.
    """
    if _is_auth_proxy_running(container_name):
        return
    start_auth_proxy(container_name)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _is_auth_proxy_running(container_name):
            return
        time.sleep(0.1)
    raise RuntimeError(
        f"auth proxy on {container_name}:{AUTH_PROXY_PORT} did not come "
        f"up within 5s; inspect with `docker exec --user "
        f"{CLAUDE_RUNNER_UID} {container_name} cat {AUTH_PROXY_LOG}`"
    )


def _read_oauth_expires_at_ms(path: str) -> Optional[int]:
    """Return claudeAiOauth.expiresAt (unix millis) from a credentials
    file, or None on any read/parse failure.

    Used to decide whether the on-disk token is wall-clock fresh —
    necessary because file mtime alone doesn't say anything about the
    token inside the file (a freshly-written file can still carry a
    long-expired bearer).
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    exp = (data or {}).get("claudeAiOauth", {}).get("expiresAt")
    if isinstance(exp, bool):
        return None  # bool is an int subclass; reject explicitly
    if isinstance(exp, (int, float)):
        return int(exp)
    return None


def _host_refresh_tokens_if_needed(account: str) -> bool:
    """If the host credentials file for ``account`` has an expired or
    near-expired access token, invoke the host-side refresh script so
    a future populate_credentials picks up a fresh bearer.

    Returns True if a refresh was attempted (whether or not it
    succeeded in advancing the token), False if the host file is
    already fresh (and the script was not invoked).

    Failure modes are tolerated quietly — the host systemd timer
    remains as the long-cadence fallback, and the downstream claude
    exit will surface a real 401 if everything is broken.
    """
    src = _host_credentials_path(account)
    exp_ms = _read_oauth_expires_at_ms(src)
    if exp_ms is None:
        # File missing or malformed; populate_credentials will surface
        # the underlying error far more usefully than a refresh attempt.
        return False
    remaining_s = (exp_ms / 1000.0) - time.time()
    if remaining_s > TOKEN_EXPIRY_GRACE_SECONDS:
        return False
    if not os.path.exists(HOST_TOKEN_REFRESH_SCRIPT):
        _log.warning(
            "host token refresh needed for account=%s (expires in %.0fs) "
            "but %s is missing — falling back to systemd timer",
            account, remaining_s, HOST_TOKEN_REFRESH_SCRIPT,
        )
        return False
    _log.info(
        "invoking %s — account=%s token expires in %.0fs",
        HOST_TOKEN_REFRESH_SCRIPT, account, remaining_s,
    )
    try:
        proc = subprocess.run(
            [HOST_TOKEN_REFRESH_SCRIPT],
            check=False,
            capture_output=True,
            timeout=HOST_TOKEN_REFRESH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        _log.error(
            "%s timed out after %.0fs", HOST_TOKEN_REFRESH_SCRIPT,
            HOST_TOKEN_REFRESH_TIMEOUT_S,
        )
        return True
    except OSError:
        _log.exception("%s failed to exec", HOST_TOKEN_REFRESH_SCRIPT)
        return True
    if proc.returncode != 0:
        _log.warning(
            "%s exited %d; stderr=%s", HOST_TOKEN_REFRESH_SCRIPT,
            proc.returncode,
            proc.stderr.decode("utf-8", "replace").strip()[:500],
        )
    return True


def _in_container_mtime(container_name: str) -> Optional[float]:
    """Return the in-container credentials file mtime as a unix timestamp,
    or None if the file is absent.

    Uses `stat -c %Y` which is portable across busybox and gnu stat. Any
    non-zero exit (including ENOENT) maps to None — callers treat None as
    "missing or unreadable, refresh".
    """
    proc = _docker_exec(
        container_name,
        ["stat", "-c", "%Y", CLAUDE_RUNNER_CREDENTIALS_PATH],
        user="root",
        check=False,
    )
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", "replace").strip()
    if not out:
        return None
    try:
        return float(out)
    except ValueError:
        return None


def refresh_credentials_if_stale(container_name: str, account: str) -> None:
    """Refresh credentials at most once per REFRESH_RATE_CAP_SECONDS.

    Stale-detection conditions that trigger a populate_credentials call
    when the rate cap allows:
      (a) in-container file is missing,
      (b) in-container file mtime > REFRESH_MAX_AGE_SECONDS old,
      (c) host source mtime is newer than in-container mtime,
      (d) the host source's stored access token is expired or within
          TOKEN_EXPIRY_GRACE_SECONDS of expiry — in which case we first
          invoke the host refresh script, then fall through to (c) so the
          freshly-rotated bearer is streamed into the container.

    (d) closes the historical dead window where the host systemd timer
    hadn't fired yet but the token had already expired: per-user
    containers now self-heal on the next turn rather than 401'ing until
    the wall-clock timer catches up.

    The cache timestamp is updated whenever the rate cap allowed us
    through, regardless of whether we ended up calling populate_credentials.
    That keeps the rate cap honest: a rapid burst of turns hits the cache
    and bypasses the (relatively expensive) docker-exec stat call.
    """
    now = time.monotonic()
    last = _REFRESH_CACHE.get(container_name)
    if last is not None and (now - last) < REFRESH_RATE_CAP_SECONDS:
        return
    # Mark BEFORE the work so a slow exec doesn't permit a parallel re-check
    # to slip past. Worst case on exception below: we record one failed
    # attempt and skip the next 60s — acceptable; the admin path notices.
    _REFRESH_CACHE[container_name] = now

    # Dynamic account resolution: the requested (session/env) account is a
    # preference; if it is saturated or dead the container is served a
    # usable account's bearer instead, and switches back once it recovers.
    requested = account
    account = resolve_usable_account(account)

    # (d) — rotate the host bearer first if it's near or past expiry.
    # The host file is what populate_credentials streams into the
    # container, so refreshing it here means the existing (c) host_mtime
    # check catches the rotation in the same pass.
    _host_refresh_tokens_if_needed(account)

    # Account switch: the mtime comparisons below can't detect that the
    # container holds a DIFFERENT account's bearer (they compare against
    # one host file), so stream explicitly when the recorded account
    # changed — OR when the record is unknown (post-restart) and the
    # resolution just switched away from the requested account: in that
    # case the container almost certainly holds the requested (unusable)
    # account's bearer, and falling through would leave it rate-limited
    # until the next host rotation. Unknown state WITHOUT a switch falls
    # through to the mtime logic (the held bearer is the right account).
    streamed = _STREAMED_ACCOUNT.get(container_name)
    if (streamed is not None and streamed != account) or (
        streamed is None and account != requested
    ):
        populate_credentials(container_name, account)
        return

    container_mtime = _in_container_mtime(container_name)
    if container_mtime is None:
        populate_credentials(container_name, account)
        return

    wall_now = time.time()
    if (wall_now - container_mtime) > REFRESH_MAX_AGE_SECONDS:
        populate_credentials(container_name, account)
        return

    src = _host_credentials_path(account)
    try:
        host_mtime = os.path.getmtime(src)
    except OSError:
        # Source unreadable from chat container — nothing we can do this
        # cycle. Don't crash the turn; admin will see the missing file via
        # the next provisioning run.
        return
    if host_mtime > container_mtime:
        populate_credentials(container_name, account)

    # Proxy presence is checked on the same rate-cap cadence as credential
    # freshness; the per-user container's restart-policy can revive the
    # container without the proxy, and we want the next turn to self-heal
    # rather than ECONNREFUSE. Cheap probe (single localhost urlopen)
    # when proxy is up; only start_auth_proxy on cold-start.
    try:
        ensure_auth_proxy_running(container_name)
    except Exception:
        # Don't crash the turn on proxy-restart trouble — log and let the
        # downstream claude exit surface a more actionable error if the
        # connection actually fails.
        _log.exception(
            "ensure_auth_proxy_running failed for %s", container_name,
        )

    # Re-stage the WORKSPACE dummy on the same rate-cap cadence. A long-lived
    # container's _provision_container only runs on (re)create, NOT per turn —
    # so a claude self-wipe of /workspace/.claude/.credentials.json (it zeroes
    # its own tokens on an auth/rate-limit failure) would otherwise persist as
    # a sticky "Not logged in" across every later turn, even after the pooled
    # account recovers. Restoring the placeholder here means the wipe self-
    # heals within REFRESH_RATE_CAP_SECONDS and turns surface the TRUE upstream
    # status (e.g. a clean rate-limit message) instead of a phantom logout.
    try:
        _write_workspace_dummy_credentials(container_name, account)
    except Exception:
        _log.exception(
            "dummy-credential restage failed for %s", container_name,
        )


# ---------------------------------------------------------------------------
# Account selection. Item 1 leaves the account-selection policy stubbed at
# "account-1" — Phase N+1 (per-user account routing) replaces this with an
# email->account lookup. The seam exists here so the call site in
# ensure_user_container does not need to change when that lookup lands.
# ---------------------------------------------------------------------------


def _account_for_email(email: str) -> str:
    """Pick whichever account chat's account_router selects at provision
    time. The original Phase 1 implementation hardcoded "account-1", but
    that account does not exist on the deployed host (only "account-2"
    is provisioned), so credential population blew up at first deploy.
    Routing through account_router keeps this consistent with how chat
    already picks an account for its own session-create path. Phase N+1
    can plug an email->account map in here for stable per-user pinning."""
    # Dual-context import: chat's image flattens these modules under /app
    # (bare-name import works), while term-router/spend/codebase bind-mount
    # the chat package at /app/services/chat (only the relative form
    # resolves). Try relative first, fall back to bare for the chat image.
    try:
        from . import account_router
    except ImportError:
        import account_router
    return account_router.pick().name


def _provision_container(
    client,
    *,
    name: str,
    volume_name: str,
    network_name: str,
    limits: dict,
    account: Optional[str],
) -> None:
    """Create-if-absent + run the post-up provisioning steps on `name`.

    ``account`` may be None when no usable Claude account was available at
    provision time. The container, volume, network, runner dirs, workspace
    sharing and auth proxy are all account-independent and still set up;
    only credential seeding (populate_credentials + dummy-cred staging) is
    skipped. The result is a fully usable workspace (dev/drive file access)
    whose claude-in-terminal stays unavailable until an account frees up and
    a later idempotent ensure-call seeds the bearer.

    Extracted from ensure_user_container so ensure_shared_container reuses
    the same docker.containers.run call shape and the same idempotent
    post-up sequence (runner dirs, workspace sharing, credential streaming,
    dummy-cred staging, auth proxy). The only per-call inputs are the
    container name, the named volume, the bridge network, resource limits,
    and the Claude account whose bearer the auth proxy will mediate.

    Per-user containers and the shared container are wire-compatible at
    this level: every difference (naming policy, account selection,
    overrides lookup) lives upstream of this helper and lands as plain
    parameters here.
    """
    # The squid egress proxy must live ON this bridge (docker isolates
    # separate bridges, and squid's source ACL only accepts the user pool
    # /16). Attach it at the reserved host on every call so the path exists
    # for both fresh and already-running containers; the resulting per-net
    # IP is what HTTPS_PROXY points at below.
    subnet = get_existing_network_subnet(client, network_name)
    proxy_ip = _egress_proxy_ip_for_subnet(subnet)
    if proxy_ip:
        _ensure_egress_proxy_attached(client, network_name, proxy_ip)
    else:
        _log.warning(
            "no parseable subnet for %s; cannot attach egress proxy or set "
            "HTTPS_PROXY — container will lack public egress under the F-1 "
            "lockdown", network_name,
        )

    try:
        client.containers.get(name)
        existed = True
    except docker.errors.NotFound:
        existed = False

    if not existed:
        env = {
            "HOME": WORKSPACE_PATH,
            "PATH": _LINUXBREW_PATH,
            # Security audit F-1 remediation (2026-05-28): per-user and
            # shared containers are iptables-DROP'd from reaching the
            # public internet. The wizerith-egress-proxy (squid with a
            # hostname allowlist, see infra/wizerith-egress-proxy/squid.conf)
            # is the single egress hole. squid is attached to THIS bridge at
            # the reserved host (see _ensure_egress_proxy_attached) because
            # docker isolates separate bridges and squid's src ACL only
            # accepts the user pool /16 — so the proxy address is per-net,
            # not the 172.20 home IP. Setting HTTPS_PROXY here makes claude
            # CLI, pip, npm, git, curl, wget, requests, urllib3 etc. route
            # through the proxy automatically. A coworker who tries to
            # bypass it via `unset HTTPS_PROXY` still hits the iptables
            # default-deny, so this env is the convenience layer rather
            # than the enforcement layer. NO_PROXY exempts the loopback
            # auth-proxy + container-internal addresses.
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        }
        if proxy_ip:
            proxy_url = f"http://{proxy_ip}:{EGRESS_PROXY_PORT}"
            env["HTTPS_PROXY"] = proxy_url
            env["HTTP_PROXY"] = proxy_url
            env["https_proxy"] = proxy_url
            env["http_proxy"] = proxy_url
        for k in _USER_CONTAINER_FORWARDED_ENV_KEYS:
            v = os.environ.get(k)
            if v:
                env[k] = v
        client.containers.run(
            image=USER_IMAGE,
            name=name,
            user="1000:1000",
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            # PID 1 must reap. Without an init, PID 1 is the `tail` below,
            # which never reaps its exited children; every claude/bash the
            # agent spawns then becomes a zombie until they hit pids_limit
            # (256) and fork() fails container-wide ("Cannot fork") — the
            # container wedges dead and only a recreate clears it (observed
            # on sophia.cheung 2026-07-10 after ~3wk uptime). init=True makes
            # Docker inject tini as PID 1 (tail runs under it), which reaps
            # zombies, so pids never accumulate. Do NOT remove.
            init=True,
            command=["tail", "-f", "/dev/null"],
            mem_limit=limits["mem_limit"],
            memswap_limit=limits["memswap_limit"],
            nano_cpus=limits["nano_cpus"],
            pids_limit=limits["pids_limit"],
            network=network_name,
            environment=env,
            # /opt/wizerith/claude-accounts is INTENTIONALLY NOT mounted
            # here. Credentials enter via populate_credentials (docker
            # exec -i --user root) so they land owned by uid/gid 2000
            # mode 0400 — unreadable by the uid 1000 user shell that
            # drops into /workspace. Same posture for shared and per-user.
            volumes={
                volume_name: {"bind": WORKSPACE_PATH, "mode": "rw"},
                LINUXBREW_HOST_PATH: {
                    "bind": LINUXBREW_CONTAINER_PATH,
                    "mode": "ro",
                },
                # Shared read-only news-engine data → Locations ▸ Database.
                # Nested under the /workspace volume; Docker applies the bind
                # after the volume mount, so it appears as /workspace/database.
                # Gated on the env var alone (truthy = enabled): the bind SOURCE
                # is resolved by the docker daemon on the HOST, so we must NOT
                # os.path.isdir() it here — this code runs inside the chat/dev
                # container, where the host path isn't visible. Set the env to
                # "" on stacks that shouldn't expose it.
                **(
                    {
                        DATABASE_MOUNT_HOST_PATH: {
                            "bind": DATABASE_MOUNT_CONTAINER_PATH,
                            "mode": "ro",
                        }
                    }
                    if DATABASE_MOUNT_HOST_PATH
                    else {}
                ),
            },
            working_dir=WORKSPACE_PATH,
            cap_drop=["ALL"],
            cap_add=["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "FOWNER", "KILL"],
            security_opt=["no-new-privileges:true"],
        )

    _setup_runner_dirs(name)
    _setup_workspace_sharing(name)
    if account is not None:
        populate_credentials(name, account)
    else:
        _log.warning(
            "%s provisioned without a Claude account; skipping REAL credential "
            "seeding — file/workspace access (dev + drive) works, but "
            "claude-in-terminal will fail until an account is available and a "
            "later ensure-call seeds the bearer",
            name,
        )
    # Stage the dummy on EVERY provision, with OR without a real account. The
    # dummy is a static placeholder (the proxy injects the real bearer), so it
    # never depends on account availability — and staging it unconditionally
    # means a claude self-wipe (it zeroes its own .credentials.json on an
    # auth/rate-limit failure) can't persist as a sticky "Not logged in":
    # the next provision restores a well-formed credential.
    _write_workspace_dummy_credentials(name, account)
    ensure_auth_proxy_running(name)


def ensure_user_container(email: str, client=None) -> str:
    """Idempotently provision a per-user container; return its name.

    Lifecycle invariants:
    - container removed → volume persists → next call recreates container with same volume
    - per-user network is a separate bridge per user; isolation comes from network separation
    - host firewall is operator-managed

    Item 1 side effects (run on EVERY call, even when container already
    existed — cheap and idempotent):
    - /var/claude-runner exists 0700 owner 2000:2000
    - /workspace is app:claude-runner mode 2775 (setgid)
    - credentials seeded at /var/claude-runner/.claude/.credentials.json
      mode 0400 owner 2000:2000
    - host claude-accounts is NOT mounted into the per-user container
      (the per-user provisioning code owns no reference to that path
      in its volumes/binds map; populate_credentials streams in via
      `docker exec -i --user root` instead).
    """
    if client is None:
        client = docker.from_env()

    name = container_name_for(email)
    volume_name = _ensure_volume(client, email)
    network_name = _ensure_network(client, email)
    limits = _resource_limits_for(email)

    # Account selection is LAZY / non-fatal here. A per-user container is a
    # workspace first and a Claude-enabled shell second: dev + drive file
    # browsing only need the container + volume to exist. A transient "no
    # usable Claude account" (e.g. every account momentarily over its 5h
    # cutoff) must NOT 500 the whole workspace. Provision without credentials
    # and let a later idempotent ensure-call (every file request makes one)
    # seed the bearer once an account frees up — only claude-in-terminal
    # degrades in the meantime. Dual-context import mirrors _account_for_email.
    try:
        from . import account_router
    except ImportError:
        import account_router
    try:
        account = _account_for_email(email)
    except account_router.NoAccountsAvailable as exc:
        account = None
        _log.warning(
            "no Claude account to seed into %s (%s); provisioning workspace "
            "without credentials so dev/drive stay up",
            name, exc,
        )

    _provision_container(
        client,
        name=name,
        volume_name=volume_name,
        network_name=network_name,
        limits=limits,
        account=account,
    )
    return name


# ---------------------------------------------------------------------------
# Shared workspace.
# ---------------------------------------------------------------------------


def shared_container_name() -> Optional[str]:
    """Return the configured shared-container name, or None if disabled.

    Reads SHARED_CONTAINER_NAME_ENV at call time so docker-compose env
    edits land without a process restart on the next request.
    """
    name = os.environ.get(SHARED_CONTAINER_NAME_ENV, "").strip()
    return name or None


def shared_container_account() -> str:
    """Return the Claude account name pinned to the shared container.

    A fixed account is essential: refresh_credentials_if_stale would
    otherwise pick a fresh account on every turn (account_router.pick),
    and concurrent users hitting the shared container could each rewrite
    the in-container .credentials.json with a different bearer mid-turn —
    the auth proxy reads that file on every request, so a swap mid-flight
    surfaces as a 401 from Anthropic for whichever turn loses the race.
    Pinning to a single account makes refreshes idempotent.

    Defaults to "main" (the bridge user's primary). Overridable via
    SHARED_CONTAINER_ACCOUNT_ENV when the operator provisions a dedicated
    shared-billing account under the wizerith pool.
    """
    return (
        os.environ.get(SHARED_CONTAINER_ACCOUNT_ENV, "").strip()
        or SHARED_CONTAINER_DEFAULT_ACCOUNT
    )


class SharedContainerNotConfigured(RuntimeError):
    """Raised when ensure_shared_container is called but
    SHARED_CONTAINER_NAME_ENV is unset on this deployment.

    Callers (chat /api/sessions, term-router /ws) translate this to a
    400 / 4400 close so the frontend can surface "shared workspace is not
    enabled on this tenant" rather than a generic 500.
    """


def ensure_shared_container(client=None) -> str:
    """Idempotently provision the tenant's single shared container.

    Same lifecycle as ensure_user_container but:
      * name comes from SHARED_CONTAINER_NAME_ENV (no per-email hashing)
      * volume / network / subnet are derived from the container name
        itself, so the allocation stays deterministic across restarts
        without burning a per-user-pool slot keyed on a fake email
      * Claude account is pinned (see shared_container_account) so
        concurrent shared turns can't race credential refreshes.

    Raises SharedContainerNotConfigured if the env var is unset — the
    deployment hasn't opted into shared workspaces.
    """
    name = shared_container_name()
    if not name:
        raise SharedContainerNotConfigured(
            f"{SHARED_CONTAINER_NAME_ENV} is unset; shared workspace is "
            "not enabled on this tenant"
        )
    if client is None:
        client = docker.from_env()

    # Volume / network names hang off the literal container name (NOT
    # the per-email hashing path) so an operator can `docker volume ls`
    # and see "<wizerith-shared>-home" matching the container name on
    # sight. We still honor _resource_limits_for / allocate_user_subnet
    # via the container name as their key — those derive from
    # _hash_for(name), which is fine because their outputs are limits
    # and a /24, not visible-named docker resources.
    volume_name = f"{name}{VOLUME_SUFFIX}"
    network_name = f"{name}{NETWORK_SUFFIX}"
    _ensure_named_volume(client, volume_name, label_value=name)
    _ensure_named_network(client, network_name, key=name, label_value=name)
    limits = _resource_limits_for(name)
    # The env pin is a preference — never provision the shared container
    # onto a saturated/dead account when a usable one exists.
    account = resolve_usable_account(shared_container_account())

    _provision_container(
        client,
        name=name,
        volume_name=volume_name,
        network_name=network_name,
        limits=limits,
        account=account,
    )
    return name


__all__ = [
    "AUTH_PROXY_LOG",
    "AUTH_PROXY_PORT",
    "CLAUDE_ACCOUNTS_HOST_PATH",
    "CLAUDE_RUNNER_CLAUDE_DIR",
    "CLAUDE_RUNNER_CREDENTIALS_PATH",
    "CLAUDE_RUNNER_GID",
    "CLAUDE_RUNNER_HOME",
    "CLAUDE_RUNNER_UID",
    "DEFAULT_CPUS",
    "DEFAULT_MEM",
    "REFRESH_MAX_AGE_SECONDS",
    "REFRESH_RATE_CAP_SECONDS",
    "SHARED_CONTAINER_ACCOUNT_ENV",
    "SHARED_CONTAINER_DEFAULT_ACCOUNT",
    "SHARED_CONTAINER_NAME_ENV",
    "SharedContainerNotConfigured",
    "USER_NETWORK_ALLOCATIONS_ENV",
    "USER_NETWORK_ALLOCATIONS_PATH_DEFAULT",
    "USER_NETWORK_POOL_CIDR",
    "USER_NETWORK_POOL_PREFIX",
    "WORKSPACE_DUMMY_CREDENTIALS_PATH",
    "WORKSPACE_MODE",
    "WORKSPACE_OWNER",
    "WORKSPACE_PATH",
    "allocate_user_subnet",
    "container_name_for",
    "ensure_auth_proxy_running",
    "ensure_shared_container",
    "ensure_user_container",
    "shared_container_account",
    "shared_container_name",
    "get_existing_network_subnet",
    "populate_credentials",
    "refresh_credentials_if_stale",
    "resolve_usable_account",
    "start_auth_proxy",
]
