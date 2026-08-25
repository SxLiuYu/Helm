#!/usr/bin/env bash
# 启动 SearXNG (本地 web research, 端口 8888)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 检测是否已在跑
if lsof -ti :8888 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "SearXNG already running on :8888"
  exit 0
fi

# macOS: .venv/bin/python, Windows: .venv/Scripts/python
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$DIR/.venv/Scripts/python"
if [ ! -x "$PY" ]; then
  echo "SearXNG venv not found. Run: cd $DIR && python3 -m venv .venv && .venv/bin/pip install searxng"
  exit 1
fi

mkdir -p data
nohup "$PY" -m searx.webapp >> data/searxng.log 2>&1 &
echo $! > data/searxng.pid
echo "SearXNG started pid=$(cat data/searxng.pid) log=$DIR/data/searxng.log"
