#!/usr/bin/env python3
"""memguard -- a userspace early-OOM killer for main-server.

Why this exists
---------------
On 2026-08-17 the host went down three times in one night. The mechanism,
confirmed from sar and the kernel OOM dump:

  * Five session containers each had a 2 GB RAM cap but an *unset*
    memswap_limit, which docker defaults to 2x mem_limit. So each could take
    2 GB RAM + 2 GB host swap. Five of them = 20 GB permitted against
    7.6 GB RAM + 8 GB swap.
  * Every container stayed comfortably under its own limit, so docker never
    OOM-killed anything. The host just swapped, then thrashed: by 03:00
    commit was 739% of RAM, page cache had collapsed 4.1 GB -> 59 MB, and
    the CPU sat at 82% iowait with 6% idle. Not busy -- blocked on disk,
    paging the same working set in and out.
  * The kernel OOM killer is the last line, not an early one. With 8 GB of
    swap to chew through it took ~20 minutes to fire, and when it did it
    picked a 100 MB claude process, which freed nothing that mattered.
    Everything stayed technically alive, so nothing self-healed and no
    watchdog tripped -- dockerd just couldn't fork health checks, and
    cloudflared couldn't reach its origins. Result: Cloudflare 1033.

The container caps are fixed at the source (user_container.py sets
memswap_limit == mem_limit, so containers can no longer touch host swap) and
the bridge units now have MemoryHigh/MemoryMax. This daemon is the backstop
for everything those two don't cover.

The normal answer here is earlyoom(8). It needs root to install and the
NOPASSWD sudo rules on this box only cover containerd/docker restart and
lvextend, so this is a stdlib-only equivalent that runs as felix. It is
deliberately not a general-purpose earlyoom clone -- it only knows how to
kill the things that actually cause this outage.

Design notes
------------
* Idle cost is near zero: /proc/meminfo is 2 reads every POLL_SEC, and the
  full per-process scan only runs once we are already above the warn line.
* It kills by RSS among a *preferred* set (claude/bun/node and friends) and
  refuses to touch the protected set (dockerd, cloudflared, caddy, postgres,
  sshd, ...). That ordering matters: an unqualified "kill the biggest thing"
  would happily take out cloudflared and hand you the same 1033 you were
  trying to prevent.
* Session containers run as user="1000:1000", i.e. felix's own uid, so their
  processes are visible in host /proc and signalable from here without root.
* MemAvailable is not sufficient on its own. On 2026-09-03 an orphaned
  `vite build` livelocked the box for 12h43m and took claude-bridge's
  Discord gateway down with it, while MemAvailable read 51.5% and swap sat
  flat at 10.1% -- both triggers below silent the whole time. The pressure
  was reclaimable *file page cache*, which MemAvailable counts as free, so
  the box refaulted the same working set forever (79.8M workingset_refault_
  file, ~13 MB/s sustained page-in) at memory PSI full=85%. PSI is the only
  tell for that shape, so it is a second, independent way into the kill
  path -- see PSI_FULL60.
* On the PSI path the victim is chosen by *livelock signature*, not by RSS:
  major faults climbing while utime does not advance. Under refault
  pressure the biggest process is often an innocent bystander, and the
  culprit may not be the largest thing on the box. The 2026-09-03 culprit
  had utime frozen at 3287 ticks across ten minutes of sampling while
  accumulating 2.69M major faults. See pick_thrasher.

* Every warn and every kill writes a process table snapshot to
  STATE_DIR/incidents.log. During the 2026-08-17 postmortem the sar data
  proved the mechanism but there was no per-process history, so the actual
  culprit could only be inferred. That gap is what this file closes.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

POLL_SEC = 2.0

# Fire while there is still enough headroom to recover. The outage window had
# MemAvailable at 11.8% and falling with swap already being written; by the
# time it reached the kernel's own threshold the box was unrecoverable.
WARN_AVAIL_PCT = 20.0
KILL_AVAIL_PCT = 12.0

# Swap is the tell. Steady-state this box swaps essentially zero (sar showed
# 0.00 pswpout/s for hours before the incident), so sustained swap growth
# combined with mild memory pressure is the early signature of the spiral.
KILL_AVAIL_PCT_WITH_SWAP = 20.0
KILL_SWAP_USED_PCT = 20.0

# The 2026-09-03 signature: sustained stall with MemAvailable looking fine.
# /proc/pressure/memory "full" is the share of wall clock in which *every*
# runnable task was stalled on memory. The outage held full avg60 in the
# 80s; a healthy box on this workload sits in the single digits. 40% is well
# clear of both. The sustain window matters as much as the level -- avg60
# decays with a ~60s time constant, so a spike, or the tail after the
# culprit dies, cannot hold 40 for two solid minutes.
PSI_FULL60 = 40.0
PSI_SUSTAIN_SEC = 120.0

# Livelock sampling, only ever paid once PSI is already sustained-high.
THRASH_SAMPLE_SEC = 3.0
THRASH_MAJFLT_MIN = 30    # >=10 major faults/sec over the sample
THRASH_UTIME_MAX = 1      # <=10ms of user CPU in 3s, i.e. no forward progress

# Give the kernel time to actually reclaim before considering another kill,
# so one pressure event doesn't cascade into killing every session at once.
KILL_COOLDOWN_SEC = 15.0

# Killed in preference order, highest RSS first within the set.
PREFER = ("claude", "bun", "node", "npm", "esbuild", "tsserver", "rg")

# Never signalled. Killing any of these turns a degraded box into a hard
# outage, which is the exact failure this daemon exists to prevent.
PROTECT = (
    "systemd", "dockerd", "containerd", "containerd-shim", "cloudflared",
    "caddy", "postgres", "sshd", "tailscaled", "containerboot", "gitea",
    "fail2ban-server", "amazon-ssm-agen", "squid", "multipathd", "init",
    "memguard.py", "python3", "uvicorn",
)

STATE_DIR = Path.home() / ".local/state/memguard"
INCIDENT_LOG = STATE_DIR / "incidents.log"


def log(msg: str) -> None:
    """stdout goes to the journal; the incident file survives a reboot."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with INCIDENT_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # never let logging failure take down the guard


def meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as f:
        for line in f:
            k, _, rest = line.partition(":")
            out[k] = int(rest.split()[0])  # kB
    return out


def pressure(mi: dict[str, int]) -> tuple[float, float]:
    total = mi.get("MemTotal", 1) or 1
    avail_pct = 100.0 * mi.get("MemAvailable", 0) / total
    sw_total = mi.get("SwapTotal", 0)
    sw_used_pct = (
        100.0 * (sw_total - mi.get("SwapFree", 0)) / sw_total if sw_total else 0.0
    )
    return avail_pct, sw_used_pct


def psi_full60() -> float:
    """memory PSI "full" avg60, or 0.0 where PSI is unavailable.

    0.0 is the right failure mode: no PSI means this trigger simply never
    fires and the daemon behaves exactly as it did before.
    """
    try:
        with open("/proc/pressure/memory", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("full"):
                    for tok in line.split():
                        if tok.startswith("avg60="):
                            return float(tok.split("=", 1)[1])
    except (OSError, ValueError):
        pass
    return 0.0


def procs() -> list[dict]:
    """Snapshot of every process we can read, with RSS in kB.

    Only called above the warn line -- see module docstring.
    """
    me = os.getpid()
    my_uid = os.getuid()
    out = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me or pid == 1:
            continue
        try:
            with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
                name = ""
                rss = 0
                uid = -1
                for line in f:
                    if line.startswith("Name:"):
                        name = line.split(maxsplit=1)[1].strip()
                    elif line.startswith("Uid:"):
                        uid = int(line.split()[1])  # effective uid
                    elif line.startswith("VmRSS:"):
                        # Field order in /proc/PID/status is Name, ... Uid,
                        # ... VmRSS, so VmRSS is the last of the three and is
                        # the only safe place to stop early. Breaking on Uid
                        # reads every process as 0 kB, which silently turns
                        # the whole daemon into a no-op. Kernel threads have
                        # no VmRSS line at all and fall through to the end of
                        # the file, which is fine -- they are filtered by the
                        # rss > 0 test in pick_victim.
                        rss = int(line.split()[1])
                        break
            if not name:
                continue
            try:
                cmd = (
                    open(f"/proc/{pid}/cmdline", "rb")
                    .read()
                    .replace(b"\x00", b" ")
                    .decode("utf-8", "replace")
                    .strip()[:160]
                )
            except OSError:
                cmd = name
            out.append(
                {"pid": pid, "name": name, "rss": rss, "uid": uid, "cmd": cmd}
            )
        except (OSError, ValueError, IndexError):
            continue  # process exited mid-scan
    return out


def snapshot(ps: list[dict], n: int = 12) -> str:
    top = sorted(ps, key=lambda p: p["rss"], reverse=True)[:n]
    rows = [
        f"    {p['rss']/1024:8.1f} MB  pid={p['pid']:<7} uid={p['uid']:<5} {p['cmd'][:110]}"
        for p in top
    ]
    return "\n".join(rows)


def pick_victim(ps: list[dict]) -> dict | None:
    """Largest signalable process from PREFER; None if there is nothing safe."""
    my_uid = os.getuid()
    cands = [
        p
        for p in ps
        if p["uid"] == my_uid
        and p["rss"] > 0
        and not any(b in p["name"] for b in PROTECT)
        and any(p["name"].startswith(g) for g in PREFER)
    ]
    if not cands:
        return None
    return max(cands, key=lambda p: p["rss"])


def thrash_stats(pid: int) -> tuple[int, int] | None:
    """(majflt, utime) in clock ticks from /proc/PID/stat, or None.

    comm is field 2 and can contain both spaces and parentheses, so fields
    are counted from the *last* ')': tail[N-3] is field N. majflt is 12 and
    utime is 14.
    """
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            raw = f.read()
        tail = raw[raw.rindex(")") + 1:].split()
        return int(tail[9]), int(tail[11])
    except (OSError, ValueError, IndexError):
        return None


def pick_thrasher(ps: list[dict]) -> dict | None:
    """The process burning the box on page-in while doing no actual work.

    Same PREFER/PROTECT/uid gate as pick_victim -- this widens *when* we
    act, never *what* we are willing to signal. An idle node process scores
    zero faults and is skipped; a genuinely busy build advances utime and is
    skipped. Only the livelocked shape matches, which is why the PSI path
    deliberately does not fall back to RSS ranking: under refault pressure
    "biggest" is not "guilty".
    """
    my_uid = os.getuid()
    cands = [
        p
        for p in ps
        if p["uid"] == my_uid
        and p["rss"] > 0
        and not any(b in p["name"] for b in PROTECT)
        and any(p["name"].startswith(g) for g in PREFER)
    ]
    if not cands:
        return None

    before = {p["pid"]: thrash_stats(p["pid"]) for p in cands}
    time.sleep(THRASH_SAMPLE_SEC)

    hits = []
    for p in cands:
        a, b = before.get(p["pid"]), thrash_stats(p["pid"])
        if a is None or b is None:
            continue  # exited mid-sample
        d_majflt, d_utime = b[0] - a[0], b[1] - a[1]
        if d_majflt >= THRASH_MAJFLT_MIN and d_utime <= THRASH_UTIME_MAX:
            hits.append(dict(p, d_majflt=d_majflt, d_utime=d_utime))
    if not hits:
        return None
    return max(hits, key=lambda p: p["d_majflt"])


def terminate(victim: dict) -> None:
    """SIGTERM, then SIGKILL if it will not die."""
    try:
        os.kill(victim["pid"], signal.SIGTERM)
        # Under this much pressure a graceful exit may never get
        # scheduled, so escalate rather than wait indefinitely.
        for _ in range(20):
            time.sleep(0.25)
            try:
                os.kill(victim["pid"], 0)
            except ProcessLookupError:
                break
        else:
            log(f"escalating pid={victim['pid']} to SIGKILL")
            try:
                os.kill(victim["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
    except (ProcessLookupError, PermissionError) as exc:
        log(f"kill failed for pid={victim['pid']}: {exc}")


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    mi = meminfo()
    log(
        f"memguard started: MemTotal={mi.get('MemTotal',0)/1048576:.1f}GB "
        f"SwapTotal={mi.get('SwapTotal',0)/1048576:.1f}GB "
        f"warn<{WARN_AVAIL_PCT}% kill<{KILL_AVAIL_PCT}% "
        f"(or <{KILL_AVAIL_PCT_WITH_SWAP}% with swap>{KILL_SWAP_USED_PCT}%) "
        f"psi_full60>{PSI_FULL60}% for {PSI_SUSTAIN_SEC:.0f}s"
    )

    last_kill = 0.0
    warned = False
    psi_since = 0.0

    while True:
        try:
            mi = meminfo()
        except OSError:
            time.sleep(POLL_SEC)
            continue

        avail, swap_used = pressure(mi)
        psi60 = psi_full60()

        # Track how long PSI has been over the line. Level alone is noisy;
        # level sustained is the signal.
        if psi60 >= PSI_FULL60:
            if psi_since == 0.0:
                psi_since = time.monotonic()
        else:
            psi_since = 0.0
        psi_hot = bool(psi_since) and (
            time.monotonic() - psi_since >= PSI_SUSTAIN_SEC
        )

        if avail >= WARN_AVAIL_PCT and not psi_hot:
            if warned:
                log(
                    f"recovered: avail={avail:.1f}% swap_used={swap_used:.1f}% "
                    f"psi_full60={psi60:.1f}%"
                )
                warned = False
            time.sleep(POLL_SEC)
            continue

        # Above the warn line: now it is worth paying for a full scan.
        ps = procs()

        if not warned:
            warned = True
            log(
                f"WARN avail={avail:.1f}% swap_used={swap_used:.1f}% "
                f"psi_full60={psi60:.1f}% procs={len(ps)} -- top by RSS:"
                f"\n{snapshot(ps)}"
            )

        # PSI path. Distinct victim selection: the livelocked process, not
        # the largest one. If nothing is livelocked we fall through to the
        # MemAvailable path rather than guessing.
        if psi_hot and time.monotonic() - last_kill >= KILL_COOLDOWN_SEC:
            thrasher = pick_thrasher(ps)
            if thrasher is not None:
                log(
                    f"KILL(thrash) psi_full60={psi60:.1f}% for "
                    f"{time.monotonic() - psi_since:.0f}s avail={avail:.1f}% -> "
                    f"SIGTERM pid={thrasher['pid']} "
                    f"rss={thrasher['rss']/1024:.1f}MB "
                    f"majflt+{thrasher['d_majflt']} utime+{thrasher['d_utime']} "
                    f"in {THRASH_SAMPLE_SEC:.0f}s {thrasher['cmd']}\n{snapshot(ps)}"
                )
                terminate(thrasher)
                last_kill = time.monotonic()
                psi_since = 0.0
                time.sleep(POLL_SEC)
                continue

        should_kill = avail < KILL_AVAIL_PCT or (
            avail < KILL_AVAIL_PCT_WITH_SWAP and swap_used > KILL_SWAP_USED_PCT
        )
        if not should_kill:
            time.sleep(POLL_SEC)
            continue

        if time.monotonic() - last_kill < KILL_COOLDOWN_SEC:
            time.sleep(POLL_SEC)
            continue

        victim = pick_victim(ps)
        if victim is None:
            log(
                f"CRITICAL avail={avail:.1f}% swap_used={swap_used:.1f}% but no "
                f"safe victim (nothing matching {PREFER} owned by uid "
                f"{os.getuid()}). Leaving it to the kernel.\n{snapshot(ps)}"
            )
            time.sleep(POLL_SEC * 2)
            continue

        log(
            f"KILL avail={avail:.1f}% swap_used={swap_used:.1f}% -> "
            f"SIGTERM pid={victim['pid']} rss={victim['rss']/1024:.1f}MB "
            f"{victim['cmd']}\n{snapshot(ps)}"
        )
        terminate(victim)

        last_kill = time.monotonic()
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
