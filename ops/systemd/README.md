# ops/systemd

User units for the host-side watchdogs. Install with:

    cp systemd/wake-watchdog.{service,timer} ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now wake-watchdog.timer

`wake-watchdog` needs a host-local config listing which sessions to watch —
`~/ops/wake_watchdog.json`, NOT tracked here because it holds per-host session
IDs. Shape:

    {"sessions": {"<session-uuid>": {"label": "...", "note": "...", "enabled": true}}}

Set `enabled: false` (or drop the entry) once a task is finished.

## session-progress

Chatty sibling of wake-watchdog: posts a progress line every 10 minutes about
sessions that are actively working. Different question, so a different unit —
wake-watchdog answers "is it broken?" (hourly, silent when healthy);
session-progress answers "what is it doing?" (frequent, always speaks). Keeping
them apart is what lets the watchdog's alerts stay rare enough to mean
something.

    cp systemd/session-progress.{service,timer} ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now session-progress.timer

Config: `~/ops/session_progress.json` (host-local, per-host session IDs).
It **self-disables** a session once that session has no pending wake and has
been idle >30 min, after sending one final "stopping reports" message.
