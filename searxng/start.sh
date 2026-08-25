#!/usr/bin/env bash
# 启动 SearXNG: 用 searxng/.venv + searxng/src (git submodule) + Helm 的 settings.yml
# 跨平台 (Windows 优先): 不依赖 lsof, 用 PID-file + kill -0 守卫
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$DIR/src"
SETTINGS="$DIR/settings.yml"
WINCOMPAT="$DIR/wincompat"
PORT=8888
PIDFILE="$DIR/data/searxng.pid"
LOGFILE="$DIR/data/searxng.log"

# 已运行则退出 (PID-file + kill -0, 跨平台)
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "SearXNG already running pid=$(cat "$PIDFILE")"
  exit 0
fi

# 选 python: Windows .venv/Scripts/python.exe, *nix .venv/bin/python
if [ -x "$DIR/.venv/Scripts/python.exe" ]; then
  PY="$DIR/.venv/Scripts/python.exe"
elif [ -x "$DIR/.venv/bin/python" ]; then
  PY="$DIR/.venv/bin/python"
else
  echo "SearXNG venv not found. 重建:" >&2
  echo "  uv venv $DIR/.venv --python 3.12" >&2
  echo "  uv pip install -r $SRC/requirements.txt --index-url https://pypi.tuna.tsinghua.edu.cn/simple" >&2
  exit 1
fi

# 路径给 python: Windows (Git Bash) 用 cygpath 转 Windows 路径 + ';' 分隔;
#   否则 (*nix) 原样 + ':' 分隔。避免 MSYS 对非 ASCII 路径的自动转换把 PYTHONPATH 搞乱。
if command -v cygpath >/dev/null 2>&1; then
  SRC_P="$(cygpath -w "$SRC")"
  WIN_P="$(cygpath -w "$WINCOMPAT")"
  SETTINGS_P="$(cygpath -w "$SETTINGS")"
  PSEP=';'
else
  SRC_P="$SRC"; WIN_P="$WINCOMPAT"; SETTINGS_P="$SETTINGS"; PSEP=':'
fi

mkdir -p "$DIR/data"
export SEARXNG_SETTINGS_PATH="$SETTINGS_P"
export PYTHONPATH="${WIN_P}${PSEP}${SRC_P}${PYTHONPATH:+${PSEP}${PYTHONPATH}}"
nohup "$PY" -m searx.webapp >> "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
echo "SearXNG started pid=$(cat "$PIDFILE") port=$PORT log=$LOGFILE"
