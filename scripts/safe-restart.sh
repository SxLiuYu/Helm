#!/usr/bin/env bash
# 安全重启 damselfish: 等在飞请求排空后再 kickstart, 避免打断进行中的 LLM 流。
#
# damselfish 是 zcode/Hermes/DSH 共用的代理, 直接 `launchctl kickstart -k`
# 会掐断所有正在流式输出的会话。本脚本先轮询 /stats 的 in_flight 计数,
# 归零(或超时)后才重启。
#
# 用法:
#   scripts/safe-restart.sh                 # 最多等 120s 排空
#   DRAIN_TIMEOUT=300 scripts/safe-restart.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.damselfish.local"
STATS_URL="${DAMSELFISH_STATS_URL:-http://127.0.0.1:8086/stats}"
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-120}"
POLL_INTERVAL=2

in_flight() {
  curl -fsS --max-time 5 "$STATS_URL" 2>/dev/null | python3 -c '
import json, sys
try:
    print(int(json.load(sys.stdin).get("in_flight", 0)))
except Exception:
    print(-1)
'
}

echo "waiting for in_flight to drain (timeout ${DRAIN_TIMEOUT}s)..."
deadline=$(( $(date +%s) + DRAIN_TIMEOUT ))
while :; do
  count="$(in_flight)"
  if [ "$count" -le 0 ] 2>/dev/null; then
    echo "drained (in_flight=${count})"
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "WARNING: drain timeout after ${DRAIN_TIMEOUT}s (in_flight=${count}), restarting anyway"
    break
  fi
  sleep "$POLL_INTERVAL"
done

echo "kickstart -k $LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

# 健康检查: 等服务重新监听
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:8086/health > /dev/null 2>&1; then
    echo "restart OK: /health responding"
    exit 0
  fi
  sleep 1
done
echo "ERROR: service did not come back within 30s; check data/damselfish.log"
exit 1
