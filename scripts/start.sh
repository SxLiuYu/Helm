#!/usr/bin/env bash
# 本地启动 damselfish: 加载 .env, 后台 uv run, PID + 日志
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

# 已运行则退出: 先 PID-file + kill -0, 再回退 netstat 找 8086 listener (跨平台, 不依赖 lsof)
if [ -f data/damselfish.pid ] && kill -0 "$(cat data/damselfish.pid)" 2>/dev/null; then
  echo "damselfish already running pid=$(cat data/damselfish.pid)"
  exit 0
fi
if [ -n "$(netstat -ano 2>/dev/null | grep -E ':8086\b' | grep -iE 'LISTENING|LISTEN' | head -1)" ]; then
  echo "damselfish already running on :8086 (port in use)"
  exit 0
fi

# damselfish 不自动 load .env, 这里 source 注入环境变量
set -a
. "$DIR/.env"
set +a

mkdir -p data
nohup uv run damselfish --config config.yml >> data/damselfish.log 2>&1 &
echo $! > data/damselfish.pid
echo "damselfish started pid=$(cat data/damselfish.pid) log=$DIR/data/damselfish.log"
