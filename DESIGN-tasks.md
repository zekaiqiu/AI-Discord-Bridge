# claude-bridge: delegated background tasks — design

Refactor of `bot.py` to support fire-and-forget background tasks (each backed by a persistent Claude session) while keeping the synchronous chat path responsive.

## What's new on the user surface

| Command | Behavior |
|---|---|
| `!task <description>` | Spawn worker now. Reply `Started t-xyz123 (worker running)`. |
| `!tasks` | List active tasks: ID, age, one-line description, last-update summary. |
| `!status <id>` | Full output of last ping + tail of subprocess log. |
| `!stop <id>` | Kill the worker (if running), keep session + log. |
| `!complete <id>` | Mark complete, archive metadata, stop pinging. Logs and session retained. |
| `!yes` | Accept the most recent suggestion (within 10 min). |
| `!no` | Reject — run the suggested prompt synchronously instead. |

### Suggested-delegation flow (regular DMs)

For DMs that don't start with `!`, run a complexity check. If complex, **don't** spawn — reply with the suggestion text and stash the pending suggestion in memory keyed by `user_id`:

```
{user_id: {"summary": str, "original_prompt": str, "expires_at": float}}
```

Pending state is consumed by `!yes` (spawn the task), `!no` (run synchronously with the original prompt), or **any other regular DM** (drop the pending and treat the new message as a fresh classification round). Bot restart drops all pending — fine, user resends.

### Complexity heuristic — proposed default

**Keyword-based.** No extra Claude call. Mark a prompt complex if any of:

- Length > 200 chars **and** contains an action verb from `{"set up", "implement", "build", "create", "research", "investigate", "scaffold", "migrate", "refactor", "audit"}`
- Numbered/sequenced phrasing matched by `r"\b(first|then|next|after that|finally)\b.*\b(then|next|after|finally)\b"` (two ordering markers)
- Length > 500 chars (long prompts almost always need delegation)
- Contains `"explore"` or `"investigate"` followed by a noun

The summary in the suggestion message is the first sentence of the prompt, truncated to 120 chars.

**Alternative considered:** ask Claude itself to classify each DM (`{complex: bool, summary: str}` JSON). More accurate, but adds 2–5 seconds and a Claude call to *every* DM. Recommend keyword for v1; add LLM classifier later if false positives/negatives are bad.

→ **Decision needed:** keyword (cheap, dumb) or LLM classifier (accurate, costly)?

## Architecture

### Task lifecycle

```
[!task X | !yes after suggestion]
        │
        ▼
generate task_id (t-<6 hex>) + session_id (uuid4)
write tasks.json entry, status="running"
        │
        ▼
spawn subprocess: claude --session-id <uuid> -p "<DECISION_PREFIX>\n\n<description>"
                           cwd=WORKING_DIR/tasks/<task_id>
                           stdout/stderr → logs/<task_id>.log
        │
        ▼
asyncio.Task tracks the proc handle in WORKERS dict
        │ (initial subprocess returns)
        ▼
status="idle"   ─── ping loop every 30 min ─── claude --resume <uuid> -p "<STATUS_PROMPT>"
                                                         │
                                                         ▼
                                          DM moderator: "[t-xyz] update: <text>"
                                          update last_ping_at
                                          if Claude says "done", DM with prompt to !complete
        │
        ▼
[!complete | !stop]
        │
        ▼
status="complete" or "stopped", move metadata to archived list, stop pinging
```

### State

`/home/felix/.local/state/claude-bridge/tasks.json`:

```json
{
  "active": [
    {
      "id": "t-a1b2c3",
      "session_id": "uuid4",
      "description": "<original prompt>",
      "created_at": 1730000000.0,
      "last_ping_at": 1730003600.0,
      "last_ping_summary": "set up X, blocked on Y",
      "status": "running|idle|stalled|stopped|complete",
      "working_dir": "/home/felix/projects/claude-bridge/work/tasks/t-a1b2c3"
    }
  ],
  "archived": [...]
}
```

Atomic writes: write to `tasks.json.tmp`, `os.replace(tmp, final)`. Single asyncio.Lock for json mutations.

### In-process state (lost on restart)

```python
WORKERS:    dict[str, asyncio.Task]              # task_id -> running asyncio task
PROCS:      dict[str, asyncio.subprocess.Process] # task_id -> handle, for !stop
PENDING:    dict[int, dict]                       # user_id -> pending suggestion
```

### Concurrency

- `MAX_ACTIVE_TASKS = 4`. `!task` and `!yes` both error if `len(active) >= 4`.
- Each worker is its own asyncio task; they don't share the synchronous `claude_lock`.
- Synchronous chat path keeps `claude_lock` exactly as today.
- Ping loop iterates active tasks sequentially (one ping at a time) to avoid hammering the Claude CLI; if a task's initial subprocess is still running, skip its ping that round.

### Worker spawn details

- **First prompt** = `DECISION_PREFIX + "\n\n" + description`. `DECISION_PREFIX` is the user's verbatim text from spec ("If you encounter a decision point that materially changes the approach…").
- **cwd** = `WORKING_DIR / "tasks" / task_id`, created at spawn. Per-task scratch space; survives across pings.
- **Args** = `claude --session-id <uuid> -p "<prompt>" --permission-mode bypassPermissions`. No `--continue` (we use explicit session ids).
- **stdout+stderr** → tee'd to `logs/<task_id>.log` (line-buffered append). Final exit code logged.
- **Status updates flow**: when subprocess exits, the asyncio task reads the tail of the log, posts `[t-id] initial run finished. exit=N. tail: …` to the moderator, sets status to `idle`, stays in the active list.

### Ping loop

- `@tasks.loop(minutes=30)`, started in `on_ready`.
- For each active task:
  - If subprocess still running → skip + log `(initial run still active)`.
  - Else `claude --resume <session_id> -p "<STATUS_PROMPT>"`, 90s timeout, capture stdout.
  - Parse output: if it contains a "done" marker, DM `[t-xyz] update: …\n_(self-reports done — !complete <id> when ready)_`.
  - Update `last_ping_at`, `last_ping_summary`.
  - If exit ≠ 0 or empty output: log a failed ping; don't update `last_ping_at`.
- Stall check: scan active tasks; any with `now - last_ping_at > 2h` AND status≠`running` → set `status="stalled"`, DM the user.

### `!stop` and `!complete`

- `!stop <id>`: if task in `WORKERS` → cancel asyncio task; if `PROCS[id]` alive → `proc.kill()`. Set status=`stopped`. Keep session and log.
- `!complete <id>`: set status=`complete`, move entry from `active` to `archived` in tasks.json. Stop pings for it. Don't delete logs or session.

### Bot restart behavior

When the bot restarts, any in-flight subprocess child died with the parent. On boot:

- Load tasks.json.
- For each `status="running"` task: it can't actually be running (parent died). Reset to `status="idle"` and DM the moderator: `[t-xyz] subprocess died with bot restart. Session preserved; pings will resume.`
- For `status="idle"` / `stopped` / `complete` / `stalled`: leave alone.
- Don't auto-respawn the initial subprocess. The user can `!task` a continuation prompt referencing the same session if they want, or just let pings drive it.

→ **Decision check:** OK to *not* auto-respawn? (Auto-respawn is doable but introduces double-execution risk if the bot was killed mid-task.)

## File / dir layout

```
/home/felix/projects/claude-bridge/
├── bot.py                          # refactored — see below
├── tasks.py                        # NEW — task store + worker mgmt
├── DESIGN-tasks.md                 # this doc
└── work/
    └── tasks/<task_id>/            # per-task scratch (lazily created)

/home/felix/.local/state/claude-bridge/
├── tasks.json                      # active + archived task metadata
└── logs/<task_id>.log              # subprocess output tee
```

`tasks.py` holds: state model, atomic write, complexity classifier, spawn logic, ping loop. `bot.py` keeps Discord glue + arena reminder. Cleaner than one 600-line `bot.py`.

## Things that stay the same

- Auth gate (only `ALLOWED_USER_ID` in DMs / mentions).
- `!new` semantics (resets `_fresh_session` flag for synchronous chat).
- `!test-arena`, arena reminder loop, all existing constants.
- Synchronous chat: same `claude --continue -p` invocation, same `claude_lock`.

## Edge cases / decisions worth flagging

1. **Complexity classifier**: keyword vs LLM. → my proposal: keyword for v1.
2. **Restart auto-respawn**: I lean *no* — just reset to idle and DM. Override?
3. **`!stop` semantics**: kill subprocess but keep session + log. Resumeable later via a fresh `!task` referencing same session id? I propose: no, `!stop` is final-ish; user can read the log. Let me know if you want a `!resume <id>`.
4. **Pings during long initial subprocess**: skip the ping that round. The first ping after the subprocess exits doubles as the "initial finished" report.
5. **DM destination**: pings DM the moderator (you) — same Discord DMChannel that the bridge already uses. We grab the user object via `client.get_user(ALLOWED_USER_ID)` and use `await user.send(...)`.
6. **Log size**: subprocess logs can grow large (Claude prints a lot). I'll cap at append-only with no rotation for v1; manual cleanup. Flag if you want a size cap.
7. **`!yes` after expiry (>10 min)**: reply `_(no pending suggestion — that one expired or was replaced)_`.
8. **`!yes` with no pending**: same reply.

## Implementation plan once approved

1. Create `tasks.py` with state model + atomic IO + classifier.
2. Refactor `bot.py`: import from `tasks.py`, add command handlers, add ping loop, add restart cleanup in `on_ready`.
3. Develop in a copy: edit `bot.py.new` and `tasks.py` in place, but only swap when validated. (Working service stays untouched until syntax-check + dry-import passes.)
4. `python3 -c 'import ast; ast.parse(open("bot.py.new").read())'` and `python3 -c "import sys; sys.path.insert(0, '.'); import tasks"` for both files.
5. Atomic swap: `mv bot.py bot.py.bak && mv bot.py.new bot.py`.
6. `systemctl --user restart claude-bridge`, tail 20 log lines, confirm bot loads cleanly, on_ready fires, ping loop registered.
7. Smoke: send `!tasks` (expect empty list), send `!task echo test` (expect `t-xxxxxx` returned), `!stop` it.

If anything in the smoke sequence fails: `mv bot.py.bak bot.py && systemctl --user restart claude-bridge` to roll back.
