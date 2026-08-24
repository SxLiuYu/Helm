#!/usr/bin/env bash
# 停止 SearXNG (按 8888 端口找 listener, taskkill /T)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=8886
PORT=8888

PID=$(netstat -ano 2>/dev/null | grep -E ":${PORT}\b" | grep -iE "LISTENING|LISTEN" | awk '{print $NF}' | head -1)

if [ -n "$PID" ]; then
  if command -v taskkill >/dev/null 2>&1; then
    taskkill //T //F //PID "$PID" 2>/dev/null && echo "stopped ${PORT} tree pid=$PID" || echo "taskkill failed for $PID"
  else
    kill "$PID" 2>/dev/null && echo "stopped pid=$PID" || echo "kill failed for $PID"
  fi
else
  echo "no :${PORT} listener"
fi
rm -f "$DIR/data/searxng.pid"
echo "SearXNG stopped"
