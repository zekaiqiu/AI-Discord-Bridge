#!/usr/bin/env python3
"""wake_watchdog -- outside observer for chat sessions driving long tasks.

Why this exists
---------------
The in-thread scheduled-wake chain is *self-terminating by design*: the model
re-schedules itself while work remains and stops once it believes the job is
done or it is genuinely blocked. That is the right behaviour, but it means a
single bad judgement ends the chain silently -- the session just goes quiet and
nothing in the system notices.

This is deliberately NOT a duplicate of the in-process machinery fixed on
2026-08-20:

  * app._requeue_failed_wake   -- a wake that FIRED and FAILED (bounded retry)
  * chat_scheduler lease       -- a wake claimed but not completed (crash/restart)
  * _may_rehome                -- a resume pointed at the wrong account HOME

All three handle a wake that went wrong. None of them handle a chain that
simply STOPPED. That is this script's only job, and it is why it runs from a
systemd timer on the host rather than from inside the chat process: an observer
that shares a failure domain with the thing it observes is not an observer.

What it does, per watched session, once an hour:

  1. Count pending wakes in the durable store for that session.
  2. Read the session's last message: timestamp, status, sequence number.
  3. Classify, and act:

     HEALTHY   a wake is pending          -> nothing to do
     WEDGED    a lease is far past due    -> report (a fire is stuck)
     STALLED   no wake + idle too long    -> re-arm one wake, so the session
                                             reports in and continues
     CAPPED    stalled repeatedly with no
               progress                   -> stop poking, log loudly

Progress (the last message sequence advancing) resets the strike count, so a
session that is working normally never accumulates strikes. A session that is
genuinely finished gets poked at most MAX_REARMS times and then left alone --
it will answer "already done", which is a cheap and self-limiting failure mode
compared to a task silently dying overnight.

Exit code is 0 unless the environment itself is broken (no docker, unreadable
store), so a red unit in `systemctl --user status` means the watchdog is blind,
not that a session is unhealthy.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request

CONTAINER = os.environ.get("WD_CONTAINER", "portfolio-chat-wizerith")
SESSIONS_ROOT = os.environ.get(
    "WD_SESSIONS_ROOT", "/home/felix/projects/chat-wizerith/sessions")
STORE = os.path.join(SESSIONS_ROOT, "_schedules.json")
STATE_PATH = os.environ.get(
    "WD_STATE", "/home/felix/ops/wake_watchdog_state.json")
CONFIG_PATH = os.environ.get(
    "WD_CONFIG", "/home/felix/ops/wake_watchdog.json")

# A session with no pending wake is only "stalled" once it has also been quiet
# this long. Generous on purpose: a turn can legitimately run 20+ minutes, and
# the fire path takes the per-session lock, so a wake queued behind a long turn
# looks like "no pending wake + recent activity" and must not trip this.
IDLE_MINUTES = 45

# A leased record whose fire has not completed within this long is wedged, not
# working. LEASE_SECONDS is 7200 so the lease itself will not lapse for hours;
# this catches it far sooner and reports rather than acting, because re-arming
# on top of a genuinely-running fire would double up.
LEASE_WEDGED_MINUTES = 30

# Human-readable echo of chat_scheduler.LEASE_SECONDS, for alert text only.
# Not imported: this runs on the host and must not depend on the container's
# module being importable.
LEASE_SECONDS_HINT = "2h after it was claimed"

# Consecutive stalls with no sequence progress before we stop poking.
MAX_REARMS = 3

REARM_PROMPT = (
    "WATCHDOG CHECK-IN (an hourly host-side timer noticed this session has no "
    "pending scheduled wake and has been idle for over {idle} minutes, so the "
    "wake chain has stopped).\n\n"
    "If the task you were working on is FINISHED: say so in one or two lines, "
    "state the final result, and do NOT schedule another wake.\n\n"
    "If it is NOT finished: say plainly what the current state is, why the "
    "chain stopped (did you decide you were blocked? did a wake fail?), then "
    "continue the work and schedule your next wake so the chain resumes. If "
    "you are blocked on something only Felix can decide, say exactly what you "
    "need from him -- do not sit silent.\n\n"
    "Be brief. This is a liveness check, not a request for a full report."
)


def _log(msg: str) -> None:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp}  {msg}", flush=True)


# --------------------------------------------------------------------------
# Notification. A journal line is not a notification -- nobody reads the
# journal at 3am. We DM Felix through the same bot the bridge uses, but by
# calling the Discord API directly rather than going through the bridge
# process: the bridge is one of the things that can be down when this fires,
# and a notifier that needs the failing system to be healthy is decoration.
# --------------------------------------------------------------------------

BRIDGE_ENV = os.environ.get(
    "WD_BRIDGE_ENV", "/home/felix/Multi-Agent-Framework/claude-bridge/.env")
_DISCORD_API = "https://discord.com/api/v10"


def _read_env_file(path: str) -> dict:
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _discord_post(path: str, payload: dict, token: str) -> dict | None:
    req = urllib.request.Request(
        _DISCORD_API + path,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (wake_watchdog, 1.0)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as fh:
        return json.load(fh)


def notify(text: str) -> bool:
    """DM the operator. Never raises, never logs the token."""
    env = _read_env_file(BRIDGE_ENV)
    token = env.get("DISCORD_BOT_TOKEN")
    user_id = env.get("ALLOWED_USER_ID")
    if not token or not user_id:
        _log("WARN cannot notify: bot token / user id not found in bridge .env")
        return False
    try:
        dm = _discord_post("/users/@me/channels", {"recipient_id": user_id}, token)
        if not dm or "id" not in dm:
            _log("WARN cannot notify: no DM channel returned")
            return False
        _discord_post(f"/channels/{dm['id']}/messages",
                      {"content": text[:1900]}, token)
        return True
    except urllib.error.HTTPError as exc:
        # Deliberately does not echo the response body -- it can contain the
        # request headers we sent.
        _log(f"WARN notify failed: HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        _log(f"WARN notify failed: {type(exc).__name__}")
    return False


def _load_json(path: str, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        _log(f"WARN could not read {path}: {exc}")
        return default


def _save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, STATE_PATH)
    except OSError as exc:
        _log(f"WARN could not persist state: {exc}")


def _find_session_file(session_id: str) -> str | None:
    """Locate <sessions>/<email-dir>/<session_id>.json without assuming the
    email->directory mangling stays as it is today."""
    try:
        for entry in os.scandir(SESSIONS_ROOT):
            if not entry.is_dir():
                continue
            candidate = os.path.join(entry.path, f"{session_id}.json")
            if os.path.exists(candidate):
                return candidate
    except OSError as exc:
        _log(f"WARN cannot scan {SESSIONS_ROOT}: {exc}")
    return None


def _parse_ts(ts: str) -> _dt.datetime | None:
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# A turn whose claude transcript was appended to this recently is doing work,
# whatever the session JSON says. Sized above the gap between tool calls on a
# slow remote command (ssh round-trips, long-running builds) so a legitimately
# quiet stretch mid-turn is not mistaken for death.
TURN_ALIVE_MINUTES = 10

ACCOUNTS_ROOT = os.environ.get(
    "WD_ACCOUNTS_ROOT", "/opt/wizerith/claude-accounts/_wizerith-ai-pool")
MAIN_HOME = os.environ.get("WD_MAIN_HOME", "/home/felix")


def _turn_is_alive(session: dict) -> bool:
    """True when the session's claude transcript is being written right now.

    The discriminator between "wedged fire" and "big turn": a stuck fire's
    transcript is static, a working one grows. The session JSON cannot tell
    them apart — it is only written on a terminal event, so both look like a
    message that has sat at 0 chars for an hour.

    Fails SAFE (False) when the transcript can't be found: unknown is treated
    as not-provably-alive, so a genuine wedge still gets reported."""
    sid = session.get("claude_session_id")
    if not sid:
        return False
    account = session.get("account")
    home = (MAIN_HOME if not account or account == "main"
            else os.path.join(ACCOUNTS_ROOT, str(account)))
    import glob as _glob
    matches = _glob.glob(
        os.path.join(home, ".claude", "projects", "*", f"{sid}.jsonl"))
    if not matches:
        return False
    try:
        age_min = (_dt.datetime.now(_dt.timezone.utc).timestamp()
                   - os.path.getmtime(matches[0])) / 60.0
    except OSError:
        return False
    return age_min <= TURN_ALIVE_MINUTES


def _rearm(email: str, session_id: str, note: str, idle_min: int) -> str | None:
    """Register a wake through the chat container's own scheduler module.

    Going through chat_scheduler.register rather than writing _schedules.json
    directly means we inherit its atomic write, its per-session cap and its
    delay clamping, and we cannot corrupt the store the live process is using.
    """
    code = textwrap.dedent(
        """
        import os, sys, json
        sys.path.insert(0, "/app")
        import chat_scheduler
        rec = chat_scheduler.register(
            os.environ["WD_EMAIL"], os.environ["WD_SID"], os.environ["WD_PROMPT"],
            delay_seconds=60, note=os.environ["WD_NOTE"])
        print(json.dumps({"id": rec["id"]}))
        """
    )
    prompt = REARM_PROMPT.format(idle=idle_min)
    try:
        proc = subprocess.run(
            ["docker", "exec", "-i",
             "-e", f"WD_EMAIL={email}",
             "-e", f"WD_SID={session_id}",
             "-e", f"WD_PROMPT={prompt}",
             "-e", f"WD_NOTE={note}",
             CONTAINER, "/usr/local/bin/python3.12", "-"],
            input=code, capture_output=True, text=True, timeout=90,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"ERROR re-arm subprocess failed: {exc}")
        return None
    if proc.returncode != 0:
        _log(f"ERROR re-arm rejected (rc={proc.returncode}): "
             f"{(proc.stderr or '').strip()[:300]}")
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])["id"]
    except (ValueError, IndexError, KeyError):
        _log(f"WARN re-arm produced unparseable output: {proc.stdout[:200]}")
        return None


def check(session_id: str, cfg: dict, store: dict, state: dict) -> None:
    label = cfg.get("label") or session_id[:8]
    if cfg.get("enabled") is False:
        _log(f"{label}: SKIPPED (disabled in config)")
        return

    path = _find_session_file(session_id)
    if path is None:
        _log(f"{label}: SKIPPED (session file not found -- deleted?)")
        return
    session = _load_json(path, {})
    messages = session.get("messages") or []
    if not messages:
        _log(f"{label}: SKIPPED (no messages)")
        return
    last = messages[-1]
    email = session.get("email")
    seq = last.get("seq")
    status = last.get("status")
    ts = _parse_ts(last.get("ts") or "")
    now = _dt.datetime.now(_dt.timezone.utc)
    idle_min = int((now - ts).total_seconds() // 60) if ts else 10 ** 6

    mine = [s for s in store.get("schedules", [])
            if s.get("session_id") == session_id]
    pending = [s for s in mine if not s.get("leased_at")]
    leased = [s for s in mine if s.get("leased_at")]

    st = state.setdefault(session_id, {"strikes": 0, "last_seq": None})
    progressed = st.get("last_seq") != seq
    if progressed:
        # Real progress clears everything, including the "already told you"
        # flags -- if it stalls again later that IS news and should re-notify.
        st["strikes"] = 0
        st["capped_notified"] = False
        st["wedged_notified"] = []
    st["last_seq"] = seq

    # A fire that claimed its lease but never completed. Report only: the
    # session lock may legitimately be held by a long turn, and re-arming on
    # top of a live fire would double it.
    told = st.setdefault("wedged_notified", [])
    working = _turn_is_alive(session)
    for rec in leased:
        held = int((now.timestamp() - float(rec.get("leased_at") or 0)) // 60)
        if held >= LEASE_WEDGED_MINUTES and working:
            # Long lease + a transcript being actively appended to = a big
            # turn, not a stuck one. Holding the lease is exactly what a
            # 50-minute tool-heavy turn is SUPPOSED to do. Without this the
            # alarm fires on healthy work and stops meaning anything.
            _log(f"{label}: lease held {held}m but transcript is live — "
                 f"long turn, not wedged")
            continue
        if held >= LEASE_WEDGED_MINUTES:
            _log(f"{label}: WEDGED wake {rec.get('id')} leased {held}m ago and "
                 f"still not completed -- a fire may be stuck")
            if rec.get("id") not in told:
                told.append(rec.get("id"))
                notify(
                    f"⚠️ **{label}** — wake `{rec.get('id')}` has been leased "
                    f"{held}m without completing. A fire looks stuck; the "
                    f"lease lapses at {LEASE_SECONDS_HINT}. Not re-arming on "
                    f"top of it.\n`journalctl --user -u wake-watchdog.service`"
                )

    if pending:
        nxt = min(s.get("next_fire", 0) for s in pending)
        when = _dt.datetime.fromtimestamp(nxt, _dt.timezone.utc).strftime("%H:%MZ")
        _log(f"{label}: HEALTHY {len(pending)} wake(s) pending, next {when}, "
             f"idle {idle_min}m, last status={status}")
        return

    if working:
        # A turn is OPEN and its transcript is live. `idle_min` here measures
        # how long the turn has been RUNNING (the message timestamp is when it
        # started, and storage is not rewritten until it ends) — it is not
        # idleness at all, so the STALLED test below is meaningless for it.
        # Re-arming now would inject a second wake into a session that is
        # mid-thought and already holds the lock. Observed 2026-08-20: without
        # this, a healthy 50-minute tool-heavy turn was declared STALLED and
        # poked.
        _log(f"{label}: OK turn running {idle_min}m, transcript live "
             f"(status={status}) -- not stalled")
        return

    if idle_min < IDLE_MINUTES:
        _log(f"{label}: OK no wake pending but active {idle_min}m ago "
             f"(status={status}) -- a turn is probably running")
        return

    if st["strikes"] >= MAX_REARMS:
        _log(f"{label}: CAPPED still stalled after {st['strikes']} re-arms with "
             f"no progress (idle {idle_min}m). NOT poking again -- this needs a "
             f"human, or set enabled=false in {CONFIG_PATH}")
        if not st.get("capped_notified"):
            st["capped_notified"] = True
            notify(
                f"🔴 **{label}** — GIVING UP. {st['strikes']} re-arms produced "
                f"no new messages; idle {idle_min}m. The wake chain is dead and "
                f"I've stopped poking it, so nothing will restart it on its "
                f"own.\nThis one needs you. Session `{session_id[:8]}`.\n"
                f"Silence it with `enabled:false` in `{CONFIG_PATH}`."
            )
        return

    st["strikes"] += 1
    _log(f"{label}: STALLED no wake pending, idle {idle_min}m, "
         f"last status={status} -- re-arming (strike {st['strikes']}/{MAX_REARMS})")
    if not email:
        _log(f"{label}: ERROR cannot re-arm, session has no email")
        notify(f"⚠️ **{label}** — stalled {idle_min}m and I cannot re-arm it: "
               f"the session record has no email. Needs you.")
        return
    new_id = _rearm(email, session_id, cfg.get("note") or "watchdog check-in",
                    idle_min)
    if new_id:
        _log(f"{label}: re-armed as wake {new_id} (fires within ~60s)")
        notify(
            f"⚠️ **{label}** stalled — no pending wake, idle {idle_min}m "
            f"(last turn `{status}`).\nRe-armed as `{new_id}`, fires within "
            f"~60s; it'll report into the thread. Strike "
            f"{st['strikes']}/{MAX_REARMS} — after that I stop and hand it to "
            f"you."
        )
    else:
        notify(
            f"🔴 **{label}** stalled {idle_min}m and the re-arm FAILED "
            f"(strike {st['strikes']}/{MAX_REARMS}). The session is not going "
            f"to restart itself. Check "
            f"`journalctl --user -u wake-watchdog.service`."
        )


def _blind(state: dict, reason: str) -> int:
    """The watchdog itself cannot see. Say so out loud -- a silent watchdog
    looks exactly like a healthy one, which is the whole failure mode we are
    here to prevent. Rate-limited so a persistently broken environment does
    not turn into an hourly DM."""
    _log(f"ERROR {reason}")
    meta = state.setdefault("_watchdog", {})
    last = float(meta.get("blind_notified_at") or 0)
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    if now - last > 6 * 3600:
        meta["blind_notified_at"] = now
        notify(f"🔴 **wake-watchdog is blind** — {reason}. It is not watching "
               f"your sessions right now.")
    _save_state(state)
    return 1


def main() -> int:
    state = _load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    cfg_blob = _load_json(CONFIG_PATH, None)
    if not cfg_blob or not cfg_blob.get("sessions"):
        return _blind(state, f"no watch config at {CONFIG_PATH}")
    store = _load_json(STORE, None)
    if store is None:
        return _blind(state, f"schedule store unreadable at {STORE}")

    for session_id, cfg in cfg_blob["sessions"].items():
        try:
            check(session_id, cfg or {}, store, state)
        except Exception as exc:  # noqa: BLE001 -- one bad session must not
            _log(f"{session_id[:8]}: ERROR check raised: {exc!r}")

    _save_state(state)
    return 0


if __name__ == "__main__":
    if "--test-notify" in sys.argv:
        ok = notify("✅ wake-watchdog notification test — this is the channel "
                    "stall alerts will arrive on.")
        _log(f"test notification {'sent' if ok else 'FAILED'}")
        sys.exit(0 if ok else 1)
    sys.exit(main())
