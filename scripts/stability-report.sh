#!/usr/bin/env bash
# damselfish 稳定性报告: 一键输出给定时间窗内的关键健康指标。
# 用法: scripts/stability-report.sh [小时数]   (默认 24)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="$DIR/data/damselfish.db"
LOG="$DIR/data/damselfish.log"
HOURS="${1:-24}"

sqlite3 -header "$DB" "
WITH win AS (SELECT strftime('%s','now') - ${HOURS}*3600 AS t0)
SELECT COUNT(*) AS attempts,
       SUM(success) AS ok,
       ROUND(100.0*SUM(success)/COUNT(*),2) || '%' AS attempt_ok_rate
FROM decisions, win WHERE created_at > t0;"
echo "--- 上游失败按类型 ---"
sqlite3 -header "$DB" "
WITH win AS (SELECT strftime('%s','now') - ${HOURS}*3600 AS t0)
SELECT COALESCE(NULLIF(status,0),200) AS up_status, COUNT(*) AS n,
       REPLACE(SUBSTR(MAX(error),1,50), char(10), ' ') AS sample
FROM decisions, win WHERE created_at > t0 AND success=0
GROUP BY up_status ORDER BY n DESC;"
echo "--- 分 target 成功率 ---"
sqlite3 -header "$DB" "
WITH win AS (SELECT strftime('%s','now') - ${HOURS}*3600 AS t0)
SELECT target_id, COUNT(*) AS att, SUM(success) AS ok,
       ROUND(100.0*SUM(success)/COUNT(*),1) || '%' AS rate
FROM decisions, win WHERE created_at > t0
GROUP BY target_id ORDER BY att DESC LIMIT 12;"
echo "--- 熔断/隔离事件 ---"
grep -cE "circuit_seconds|sliding-window quarantine" "$LOG" || true
echo "--- 近期重启次数(按日志内 startup 行) ---"
grep "pruned.*on startup" "$LOG" | tail -200 | awk -v d="$(date -v-${HOURS}H '+%Y-%m-%d %H:%M')" '$0 >= d' | wc -l | xargs echo "窗口内约:"
echo "--- 客户端可见错误(整个当前日志文件) ---"
grep '"POST /v1/chat/completions' "$LOG" | grep -oE 'HTTP/1.1" [0-9]{3}' | awk '{print $2}' | sort | uniq -c | sort -rn
