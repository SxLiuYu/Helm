#!/usr/bin/env bash
# damselfish 保活看门狗: 每 30s 检查 8086 端口, 挂了自动重启
# 用法: nohup bash scripts/watchdog.sh &  (或开机自启)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=8086
INTERVAL=30
LOG="$DIR/data/watchdog.log"

mkdir -p "$DIR/data"

check_port() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti ":${PORT}" -sTCP:LISTEN >/dev/null 2>&1
  else
    netstat -ano 2>/dev/null | grep -qE ":${PORT}.*LISTEN"
  fi
}

while true; do
  if ! check_port; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] :${PORT} down, restarting..." >> "$LOG"
    bash "$DIR/scripts/start.sh" >> "$LOG" 2>&1
    sleep 10
    if check_port; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] restart OK" >> "$LOG"
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] restart FAILED" >> "$LOG"
    fi
  fi
  sleep "$INTERVAL"
done