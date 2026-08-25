#!/usr/bin/env bash
# 一键启动 damselfish (8086) + SearXNG (8888)。幂等：已在跑则跳过。
# 供任务计划程序(autostart.bat)或手动调用。各子脚本自带 PID-file + kill-0 守卫。
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

# 任务计划程序拉起的 bash 可能不带用户 PATH，确保 uv 可见
if ! command -v uv >/dev/null 2>&1; then
  for d in \
    "/c/Users/${USERNAME:-$USER}/AppData/Local/Programs/Python/Python39/Scripts" \
    "/c/Users/${USERNAME:-$USER}/AppData/Local/Programs/Python/Python312/Scripts" \
    "$HOME/.local/bin"; do
    if [ -x "$d/uv" ]; then export PATH="$d:$PATH"; break; fi
  done
fi

echo "[start-all] $(date '+%F %T') starting damselfish + SearXNG ..."
bash scripts/start.sh          # damselfish, 8086 (PID+kill-0 守卫)
bash searxng/start.sh          # SearXNG, 8888 (PID+kill-0 守卫)
echo "[start-all] done."
