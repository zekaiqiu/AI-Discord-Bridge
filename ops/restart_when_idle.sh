#!/bin/bash
# Restart the Discord bridge once it has been idle (no child processes =>
# no in-flight turn) for a sustained window. Logs to ops/restart_when_idle.log.
BOT_PID=$(systemctl --user show claude-bridge.service -p MainPID --value)
LOG=/home/felix/Multi-Agent-Framework/claude-bridge/ops/restart_when_idle.log
echo "$(date -u +%FT%TZ) waiting for bridge (pid $BOT_PID) to go idle" >> "$LOG"
idle=0
for i in $(seq 1 270); do   # max ~45 min
  nkids=$(ps --ppid "$BOT_PID" -o pid= 2>/dev/null | wc -l)
  if [ "$nkids" -eq 0 ]; then
    idle=$((idle+1))
    [ "$idle" -ge 4 ] && break    # ~40s of continuous idle
  else
    idle=0
  fi
  sleep 10
done
echo "$(date -u +%FT%TZ) restarting (idle_streak=$idle, kids=$nkids)" >> "$LOG"
systemctl --user restart claude-bridge.service
sleep 8
state=$(systemctl --user is-active claude-bridge.service)
echo "$(date -u +%FT%TZ) post-restart state: $state" >> "$LOG"
journalctl --user -u claude-bridge.service --since "-1 min" | grep -m1 "token alert loop" >> "$LOG" 2>&1
