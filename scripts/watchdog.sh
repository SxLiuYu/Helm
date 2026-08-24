#!/usr/bin/env bash
# damselfish 保活看门狗: 每 30s 检查 3086 端口, 挂了自动重启
# 用法: nohup bash scripts/watchdog.sh &  (或开机自启)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=3086
INTERVAL=30
LOG="$DIR/data/watchdog.log"

mkdir -p "$DIR/data"

while true; do
  if ! netstat -ano 2>/dev/null | grep -qE ":${PORT}.*LISTEN"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] :${PORT} down, restarting..." >> "$LOG"
    bash "$DIR/scripts/start.sh" >> "$LOG" 2>&1
    sleep 10
    if netstat -ano 2>/dev/null | grep -qE ":${PORT}.*LISTEN"; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] restart OK" >> "$LOG"
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] restart FAILED" >> "$LOG"
    fi
  fi
  sleep "$INTERVAL"
done
