#!/usr/bin/env bash
# 本地停止 damselfish: 找 8086 端口占用者, taskkill /T 整棵进程树
# (start.sh 的 $! 是 nohup bash PID, kill 它杀不掉 uv->python 子进程,
#  所以按端口找真正的 listener PID 更可靠)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PID=$(netstat -ano 2>/dev/null | grep -E ":8086\b" | grep -iE "LISTENING|LISTEN" | awk '{print $NF}' | head -1)

if [ -n "$PID" ]; then
  if command -v taskkill >/dev/null 2>&1; then
    taskkill //T //F //PID "$PID" 2>/dev/null && echo "stopped 8086 tree pid=$PID" || echo "taskkill failed for $PID"
  else
    kill "$PID" 2>/dev/null && echo "stopped pid=$PID" || echo "kill failed for $PID"
  fi
else
  echo "no :8086 listener"
fi
rm -f "$DIR/data/damselfish.pid"
echo "damselfish stopped"
