#!/usr/bin/env bash
# 停止 SearXNG: 先按 PID-file, 再回退 netstat 找 8888 listener
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8888
PIDFILE="$DIR/data/searxng.pid"

PID=""
if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE" 2>/dev/null || true)"
fi
# 回退: 按端口找 listener (PID-file 缺失或进程已死但端口仍占)
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
  PID="$(netstat -ano 2>/dev/null | grep -E ":${PORT}\b" | grep -iE "LISTENING|LISTEN" | awk '{print $NF}' | head -1)"
fi

if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  if command -v taskkill >/dev/null 2>&1; then
    taskkill //T //F //PID "$PID" 2>/dev/null && echo "stopped ${PORT} tree pid=$PID" || echo "taskkill failed for $PID"
  else
    kill "$PID" 2>/dev/null && echo "stopped pid=$PID" || echo "kill failed for $PID"
  fi
else
  echo "no :${PORT} listener"
fi
rm -f "$PIDFILE"
echo "SearXNG stopped"
