# claude-bridge

Discord bot + ops HTTP API that wraps the multi-agent-pipeline orchestrator
and a per-task Claude Code worker pool.

## Layout

- `bot.py` — Discord client; routes `!agent task / !agent project / !agents / !usage / !schedule` etc. to the core handlers.
- `tasks.py` — single-Opus background task workers (file-locked tasks.json + per-task PTYs).
- `bridge_api/` — FastAPI service mounted at `/api/*` (Caddy reverse-proxies `/api/ops/*` to it from the chat web UI). Same handlers as the Discord side, JSON in/out, CF Access JWT-gated.
- `agent_handles.py`, `agent_state.py`, `confirmations.py`, `quotas.py`, `scheduler.py`, `state_store.py`, `pricing.py`, `usage_report.py` — supporting modules.
- `bridge_account_router.py` — multi-account HOME routing for per-user dispatch.
- `tests/` — pytest suite.

## Deployment

Two systemd user units (live in `~/.config/systemd/user/`, not in this repo):

- `claude-bridge.service` — runs `bot.py` (the Discord client).
- `claude-bridge-api.service` — runs `python -m bridge_api` on `0.0.0.0:8765`.

Runtime state lives **outside** the repo at:

- `~/.local/state/claude-bridge/{tasks,handles}.json` — task / project records.
- `~/.local/state/claude-bridge/logs/` — per-task worker logs.
- `~/multi-agent-pipeline/` — pipeline projects (separate tree).

## Setup

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env  # fill in DISCORD_BOT_TOKEN, ALLOWED_USER_ID, CF_ACCESS_AUD, CF_ACCESS_TEAM_SUBDOMAIN, ADMIN_EMAILS
systemctl --user daemon-reload
systemctl --user enable --now claude-bridge.service claude-bridge-api.service
```

## Tests

```bash
venv/bin/pytest tests/
```
