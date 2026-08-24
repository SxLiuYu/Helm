#!/usr/bin/env bash
# 启动 SearXNG (本地 web research, 端口 8888)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

# 检测是否已在跑
if netstat -ano 2>/dev/null | grep -qE ":8888.*LISTEN"; then
  echo "SearXNG already running on :8888"
  exit 0
fi

mkdir -p data
nohup .venv/Scripts/python -m searx.webapp >> data/searxng.log 2>&1 &
echo $! > data/searxng.pid
echo "SearXNG started pid=$(cat data/searxng.pid) log=$DIR/data/searxng.log"
