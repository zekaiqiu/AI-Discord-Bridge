"""Reports and alerts over the token ledger (services/chat/token_ledger.py).

Used three ways:
  * ``!tokens ...`` in Discord (bot.py calls ``render(args)``),
  * ``token-report ...`` on the host (``python3 token_report.py ...``),
  * the bridge's alert loop (``check_alerts()`` every few minutes).

Read-only against the ledger; opens it with a short-lived connection.

Commands (window defaults to 24h; accepts 1h / 24h / 7d / 30d / today / all):
  summary [window]           totals by app, model, provider, user + cache hit
  top [window] [n]           largest turns
  turn <turn_id|prefix>      every call of one turn
  calls [window] [n]         most recent calls
  hourly [window]            spend per hour
  users [window]             per-user breakdown
  errors [window]            failed / cancelled / estimated calls
  running                    turns still in flight
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = "/home/felix/.local/state/token-ledger/ledger.db"
ALERT_STATE = Path(os.environ.get(
    "TOKEN_ALERT_STATE", "/home/felix/.local/state/token-ledger/alerts.json"))

# Alert thresholds (env-overridable). A turn trips once; a day trips once per
# threshold. Costs are list-price equivalents (see token_ledger docstring).
TURN_TOKENS = int(os.environ.get("TOKEN_ALERT_TURN_TOKENS", "10000000"))
TURN_USD = float(os.environ.get("TOKEN_ALERT_TURN_USD", "25"))
DAY_TOKENS = int(os.environ.get("TOKEN_ALERT_DAY_TOKENS", "200000000"))
DAY_USD = float(os.environ.get("TOKEN_ALERT_DAY_USD", "200"))
TURN_CALLS = int(os.environ.get("TOKEN_ALERT_TURN_CALLS", "400"))


def _db() -> sqlite3.Connection | None:
    path = os.environ.get("TOKEN_LEDGER_DB", DEFAULT_DB)
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _since(window: str) -> tuple[str, str]:
    """(iso lower bound, label). Ledger timestamps are UTC ISO strings."""
    now = datetime.now(timezone.utc)
    w = (window or "24h").lower()
    if w == "all":
        return "0000", "all time"
    if w == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.isoformat(), "today (UTC)"
    try:
        n, unit = int(w[:-1]), w[-1]
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n),
                 "d": timedelta(days=n)}[unit]
    except (ValueError, KeyError):
        raise ValueError(f"bad window {window!r} (use 1h, 24h, 7d, 30d, today, all)")
    return (now - delta).isoformat(), f"last {w}"


def _is_window(s: str) -> bool:
    try:
        _since(s)
        return True
    except ValueError:
        return False


def fmt_n(n) -> str:
    n = int(n or 0)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            return f"{n / div:.2f}{suf}"
    return str(n)


def fmt_usd(v) -> str:
    return "n/a" if v is None else (f"${v:,.2f}" if v >= 0.01 or v == 0 else f"${v:.4f}")


def _pct(a, b) -> str:
    return f"{100 * (a or 0) / b:.0f}%" if b else "-"


def _table(headers: list[str], rows: list[list], align: str | None = None) -> str:
    cells = [[str(h) for h in headers]] + [[str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    align = align or ("l" + "r" * (len(headers) - 1))
    out = []
    for j, r in enumerate(cells):
        out.append("  ".join(c.ljust(w) if a == "l" else c.rjust(w)
                             for c, w, a in zip(r, widths, align)))
        if j == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


_SUMS = """COUNT(*) calls, COUNT(DISTINCT turn_id) turns,
    SUM(input_tokens) inp, SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw,
    SUM(output_tokens) outp, SUM(reasoning_tokens) rsn, SUM(total_tokens) tot,
    SUM(cost_usd) cost, SUM(cost_usd IS NULL) unpriced, SUM(estimated) est"""


def _group_rows(conn, since: str, key: str) -> list[list]:
    rows = conn.execute(
        f"SELECT {key} k, {_SUMS} FROM calls WHERE ts_start >= ? GROUP BY {key} "
        "ORDER BY tot DESC", (since,)).fetchall()
    out = []
    for r in rows:
        prompt = (r["inp"] or 0) + (r["cr"] or 0) + (r["cw"] or 0)
        cost = fmt_usd(r["cost"]) if r["unpriced"] < r["calls"] else "n/a"
        if 0 < r["unpriced"] < r["calls"]:
            cost += "*"
        out.append([r["k"] or "-", r["turns"], r["calls"], fmt_n(r["inp"]), fmt_n(r["cr"]),
                    fmt_n(r["cw"]), fmt_n(r["outp"]), fmt_n(r["rsn"]), fmt_n(r["tot"]),
                    _pct(r["cr"], prompt), cost])
    return out


_GH = ["", "turns", "calls", "input", "cache_rd", "cache_wr", "output", "reason", "total",
       "hit", "cost"]


def summary(window: str = "24h") -> str:
    conn = _db()
    if conn is None:
        return "Token ledger is empty (no calls recorded yet)."
    since, label = _since(window)
    t = conn.execute(f"SELECT {_SUMS}, MAX(input_tokens+cache_read_tokens+cache_write_tokens) "
                     "maxctx FROM calls WHERE ts_start >= ?", (since,)).fetchone()
    if not t["calls"]:
        return f"No model calls in the {label}."
    prompt = (t["inp"] or 0) + (t["cr"] or 0) + (t["cw"] or 0)
    parts = [
        f"TOKENS -- {label}",
        f"total {fmt_n(t['tot'])} over {t['calls']} calls / {t['turns']} turns   "
        f"list-price cost {fmt_usd(t['cost'])}"
        + (f" ({t['unpriced']} calls unpriced)" if t["unpriced"] else ""),
        f"input {fmt_n(t['inp'])} + cache read {fmt_n(t['cr'])} + cache write "
        f"{fmt_n(t['cw'])} -> output {fmt_n(t['outp'])} (reasoning {fmt_n(t['rsn'])})",
        f"cache hit {_pct(t['cr'], prompt)}   largest single request "
        f"{fmt_n(t['maxctx'])} tokens   estimated calls {t['est']}",
        "",
        "BY APP", _table(_GH, _group_rows(conn, since, "app")), "",
        "BY MODEL", _table(_GH, _group_rows(conn, since, "model")), "",
        "BY USER", _table(_GH, _group_rows(conn, since, "user")), "",
        "BY PURPOSE", _table(_GH, _group_rows(conn, since, "purpose")),
        "",
        "cost: list-price equivalent; n/a = no price set for that model "
        "(~/.config/token-ledger/prices.json); * = partly unpriced",
    ]
    return "\n".join(parts)


def users(window: str = "24h") -> str:
    conn = _db()
    if conn is None:
        return "Token ledger is empty."
    since, label = _since(window)
    return f"USERS -- {label}\n" + _table(_GH, _group_rows(conn, since, "user"))


def top(window: str = "24h", n: int = 15) -> str:
    conn = _db()
    if conn is None:
        return "Token ledger is empty."
    since, label = _since(window)
    rows = conn.execute(
        "SELECT * FROM turns WHERE ts_start >= ? ORDER BY total_tokens DESC LIMIT ?",
        (since, n)).fetchall()
    if not rows:
        return f"No turns in the {label}."
    body = [[r["turn_id"][-22:], r["ts_start"][5:16].replace("T", " "), r["app"],
             (r["user"] or "-")[:24], (r["model"] or "-")[:18], r["status"],
             r["n_calls"], r["n_tool_calls"], fmt_n(r["total_tokens"]),
             fmt_n(r["output_tokens"]), fmt_usd(r["cost_usd"]),
             f"{(r['duration_ms'] or 0) / 60000:.1f}m"] for r in rows]
    return f"TOP TURNS -- {label}\n" + _table(
        ["turn", "start", "app", "user", "model", "status", "calls", "tools", "total",
         "output", "cost", "dur"], body, "llllllrrrrrr")


def turn(turn_id: str) -> str:
    conn = _db()
    if conn is None:
        return "Token ledger is empty."
    t = conn.execute("SELECT * FROM turns WHERE turn_id = ? OR turn_id LIKE ? "
                     "ORDER BY ts_start DESC LIMIT 1",
                     (turn_id, f"%{turn_id}%")).fetchone()
    if t is None:
        return f"No turn matching {turn_id!r}."
    calls = conn.execute("SELECT * FROM calls WHERE turn_id = ? ORDER BY id",
                         (t["turn_id"],)).fetchall()
    head = (
        f"TURN {t['turn_id']}\n"
        f"{t['app']}  user={t['user']}  session={t['session']}\n"
        f"model={t['model']} ({t['provider']})  effort={t['effort']}  account={t['account']}\n"
        f"{t['ts_start']} -> {t['ts_end'] or 'running'}  status={t['status']}  "
        f"{(t['duration_ms'] or 0) / 1000:.0f}s\n"
        f"{t['n_calls']} calls, {t['n_tool_calls']} tool calls, total {fmt_n(t['total_tokens'])} "
        f"(in {fmt_n(t['input_tokens'])}, cache rd {fmt_n(t['cache_read_tokens'])}, "
        f"cache wr {fmt_n(t['cache_write_tokens'])}, out {fmt_n(t['output_tokens'])}, "
        f"reasoning {fmt_n(t['reasoning_tokens'])})\n"
        f"cost {fmt_usd(t['cost_usd'])}"
        + (f"   CLI-reported {fmt_usd(t['cli_cost_usd'])}" if t["cli_cost_usd"] is not None else "")
        + (f"   {t['estimated_calls']} estimated call(s)" if t["estimated_calls"] else "")
    )
    body = [[c["call_idx"] or "-", c["ts_start"][11:19], (c["purpose"] or "-")[:10],
             (c["model"] or "-")[:18], c["status"] + ("~" if c["estimated"] else ""),
             fmt_n(c["input_tokens"]), fmt_n(c["cache_read_tokens"]),
             fmt_n(c["cache_write_tokens"]), fmt_n(c["output_tokens"]),
             fmt_n(c["reasoning_tokens"]), c["tool_calls"] or 0,
             f"{(c['ttft_ms'] or 0) / 1000:.1f}", f"{(c['latency_ms'] or 0) / 1000:.1f}",
             fmt_usd(c["cost_usd"])] for c in calls]
    return head + "\n\n" + _table(
        ["#", "time", "purpose", "model", "status", "input", "cache_rd", "cache_wr",
         "output", "reason", "tools", "ttft", "secs", "cost"], body,
        "rlllrrrrrrrrrr") + "\n(~ = estimated from characters; the provider sent no usage)"


def calls(window: str = "24h", n: int = 30) -> str:
    conn = _db()
    if conn is None:
        return "Token ledger is empty."
    since, label = _since(window)
    rows = conn.execute("SELECT * FROM calls WHERE ts_start >= ? ORDER BY id DESC LIMIT ?",
                        (since, n)).fetchall()
    body = [[c["ts_start"][5:19].replace("T", " "), c["app"] or "-", (c["user"] or "-")[:20],
             (c["model"] or "-")[:18], c["status"] + ("~" if c["estimated"] else ""),
             fmt_n(c["input_tokens"]), fmt_n(c["cache_read_tokens"]), fmt_n(c["output_tokens"]),
             fmt_n(c["total_tokens"]), f"{(c['latency_ms'] or 0) / 1000:.1f}s",
             fmt_usd(c["cost_usd"])] for c in rows]
    return f"RECENT CALLS -- {label}\n" + _table(
        ["time", "app", "user", "model", "status", "input", "cache_rd", "output", "total",
         "secs", "cost"], body, "lllllrrrrrr")


def hourly(window: str = "24h") -> str:
    conn = _db()
    if conn is None:
        return "Token ledger is empty."
    since, label = _since(window)
    rows = conn.execute(
        f"SELECT substr(ts_start,1,13) k, {_SUMS} FROM calls WHERE ts_start >= ? "
        "GROUP BY k ORDER BY k", (since,)).fetchall()
    peak = max((r["tot"] or 0 for r in rows), default=0)
    body = [[r["k"].replace("T", " ") + "h", r["calls"], fmt_n(r["tot"]), fmt_n(r["outp"]),
             fmt_usd(r["cost"]), "#" * int(30 * (r["tot"] or 0) / peak) if peak else ""]
            for r in rows]
    return f"HOURLY -- {label} (UTC)\n" + _table(
        ["hour", "calls", "total", "output", "cost", ""], body, "lrrrrl")


def errors(window: str = "24h") -> str:
    conn = _db()
    if conn is None:
        return "Token ledger is empty."
    since, label = _since(window)
    rows = conn.execute(
        "SELECT status, estimated, model, COUNT(*) n, SUM(total_tokens) tot, "
        "MAX(error) err FROM calls WHERE ts_start >= ? AND (status != 'ok' OR estimated) "
        "GROUP BY status, estimated, model ORDER BY n DESC", (since,)).fetchall()
    if not rows:
        return f"No failed, cancelled or estimated calls in the {label}."
    body = [[r["status"], "yes" if r["estimated"] else "", r["model"] or "-", r["n"],
             fmt_n(r["tot"]), (r["err"] or "")[:60]] for r in rows]
    return f"NON-OK CALLS -- {label}\n" + _table(
        ["status", "est", "model", "calls", "tokens", "sample error"], body, "lllrrl")


def running() -> str:
    conn = _db()
    if conn is None:
        return "Token ledger is empty."
    rows = conn.execute("SELECT * FROM turns WHERE status = 'running' ORDER BY ts_start").fetchall()
    if not rows:
        return "No turns in flight."
    body = [[r["turn_id"][-22:], r["ts_start"][5:16].replace("T", " "), r["app"],
             (r["user"] or "-")[:24], r["model"] or "-", r["n_calls"],
             fmt_n(r["total_tokens"]), fmt_usd(r["cost_usd"])] for r in rows]
    return "RUNNING TURNS (a process restart can leave stale rows here)\n" + _table(
        ["turn", "start", "app", "user", "model", "calls", "total", "cost"], body, "lllllrrr")


def render(argv: list[str]) -> str:
    """Dispatch a command line (list of words) to a report."""
    if not argv:
        return summary("24h")
    cmd, rest = argv[0].lower(), argv[1:]
    if _is_window(cmd):
        return summary(cmd)
    win = next((a for a in rest if _is_window(a)), "24h")
    num = next((int(a) for a in rest if a.isdigit()), None)
    if cmd == "summary":
        return summary(win)
    if cmd == "top":
        return top(win, num or 15)
    if cmd == "turn":
        return turn(rest[0]) if rest else "usage: turn <turn_id>"
    if cmd == "calls":
        return calls(win, num or 30)
    if cmd == "hourly":
        return hourly(win)
    if cmd == "users":
        return users(win)
    if cmd == "errors":
        return errors(win)
    if cmd == "running":
        return running()
    return __doc__.split("Commands", 1)[1].strip()


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def check_alerts(state_path: Path = ALERT_STATE) -> list[str]:
    """New alert messages since the last check (each fires once)."""
    conn = _db()
    if conn is None:
        return []
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    fired: dict = state.setdefault("fired", {})
    out: list[str] = []
    day_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    for r in conn.execute(
            "SELECT * FROM turns WHERE ts_start >= ? AND (total_tokens >= ? OR "
            "COALESCE(cost_usd,0) >= ? OR n_calls >= ?)",
            (day_ago, TURN_TOKENS, TURN_USD, TURN_CALLS)):
        key = f"turn:{r['turn_id']}"
        if key in fired:
            continue
        fired[key] = datetime.now(timezone.utc).isoformat()
        out.append(
            f"**Token alert: large turn** ({r['status']})\n"
            f"{r['app']} / {r['user']} / {r['model']}\n"
            f"{fmt_n(r['total_tokens'])} tokens over {r['n_calls']} calls "
            f"({fmt_n(r['output_tokens'])} output), cost {fmt_usd(r['cost_usd'])}\n"
            f"thresholds: {fmt_n(TURN_TOKENS)} tokens / {fmt_usd(TURN_USD)} / {TURN_CALLS} calls\n"
            f"`!tokens turn {r['turn_id']}`")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = conn.execute(f"SELECT {_SUMS} FROM calls WHERE ts_start >= ?", (today,)).fetchone()
    for what, val, lim, show in (("tokens", d["tot"] or 0, DAY_TOKENS, fmt_n),
                                 ("cost", d["cost"] or 0, DAY_USD, fmt_usd)):
        key = f"day:{today}:{what}"
        if val >= lim and key not in fired:
            fired[key] = datetime.now(timezone.utc).isoformat()
            out.append(f"**Token alert: daily {what}** {show(val)} today (UTC) crossed "
                       f"{show(lim)}. `!tokens today`")
    # Forget keys older than a week.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    state["fired"] = {k: v for k, v in fired.items() if v >= cutoff}
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(state_path)
    except OSError:
        pass
    return out


if __name__ == "__main__":
    print(render(sys.argv[1:]))
