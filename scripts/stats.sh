#!/usr/bin/env bash
# 一键查看 damselfish 三指标: 智能分数 + 成功率 + 超时时间
# 用法: bash scripts/stats.sh
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

uv run python - <<'PYEOF'
import sqlite3, time, yaml, os, sys

cfg = yaml.safe_load(open("config.yml", encoding="utf-8"))
routing = cfg.get("routing", {})
timeout = routing.get("request_timeout_seconds", "?")
connect = routing.get("connect_timeout_seconds", "?")
probe = routing.get("probe_interval_seconds", "?")

# intelligence 从 config targets 读; 成功率/延迟从 SQLite 读
intel = {}
for t in cfg.get("targets", []):
    if t.get("enabled", True) and t.get("intelligence"):
        intel[t["id"]] = t["intelligence"]

db = "data/damselfish.db"
if not os.path.exists(db):
    print("damselfish.db not found (服务未跑过?)"); sys.exit(1)

c = sqlite3.connect(db)
c.row_factory = sqlite3.Row
rows = c.execute("""
    SELECT target_id, requests, successes, failures, rate_limits,
           last_latency_ms, ewma_latency_ms, circuit_open_until
    FROM target_stats ORDER BY target_id
""").fetchall()
now = time.time()

print(f"damselfish 路由统计  (request_timeout={timeout}s  connect_timeout={connect}s  probe={probe}s)")
print(f"{'target':<28} {'intel':>5} {'req':>4} {'ok':>4} {'fail':>4} {'rate':>4} {'成功率':>6} {'last_ms':>7} {'ewma_ms':>7} {'熔断':>4}")
print("-" * 90)
for r in rows:
    tid = r["target_id"]
    req = r["requests"]
    ok = r["successes"]
    rate_pct = f"{ok*100//req}%" if req else "-"
    ci = "是" if r["circuit_open_until"] and r["circuit_open_until"] > now else ""
    i = intel.get(tid, "-")
    print(f"{tid:<28} {str(i):>5} {req:>4} {ok:>4} {r['failures']:>4} {r['rate_limits']:>4} {rate_pct:>6} {(r['last_latency_ms'] or 0):>7.0f} {(r['ewma_latency_ms'] or 0):>7.0f} {ci:>4}")
c.close()
PYEOF
