# Helm — zcode + damselfish + SearXNG

## 架构

```
zcode (harness) ──default model──> damselfish (127.0.0.1:8086) ──智能路由──> 多个免费上游
  │                                     │
  │ Bash/Edit/Read/Grep/Glob             │ 场景+人物+延迟+失败率 综合评分
  │ Agent subagents (并行/编排)          │ 三阶段回退: 串行→竞速→兜底
  │ Skills (orchestration 等)           │ SQLite 记忆 · 熔断 · 健康检查
  │                                     │
  └── SearXNG (127.0.0.1:8888)          └── 14 个 chat 上游 (agnes/finna/stepfun)
      in-Helm venv, JSON API
```

## 模型

zcode 的 LLM provider 配置为 damselfish (`damselfish/auto`)，所有请求先到本地 damselfish，由 damselfish 按场景、延迟、失败率综合评分选最优上游。

zcode 不发送 `X-Damselfish-Scenario`/`X-Damselfish-Persona` 头，damselfish 通过 `infer_context()` 从消息内容自动推断场景和角色。

## 关键路径

| 组件 | 位置 | 端口 |
|---|---|---|
| zcode config | `~/.zcode/v2/config.json` (provider UUID `13f8b742-...`) | — |
| zcode settings | `~/.zcode/v2/setting.json` (`providerFamilyDomain` = damselfish UUID) | — |
| damselfish | Helm 根目录, `bash scripts/start.sh`（Windows 自启 `autostart.vbs` / macOS launchd） | 8086 |
| SearXNG | `searxng/`（git submodule `src/` + 自带 `.venv`，`searxng/start.sh`） | 8888 |
| damselfish config | `config.yml` (gitignore) | — |
| API keys | `.env` (gitignore) | — |

## 启停

```bash
# damselfish (8086) — Windows 开机自启: wscript scripts/autostart.vbs install
bash scripts/start.sh          # 手动启动
bash scripts/stop.sh           # 手动停止

# SearXNG (8888, in-Helm venv, 详见 searxng/README.md)
bash searxng/start.sh          # 启动
bash searxng/stop.sh           # 停止
curl -s "http://127.0.0.1:8888/search?q=test&format=json" | python3 -m json.tool  # 验证

# 一键启动两者（幂等）
bash scripts/start-all.sh
```

## 多角色协作

dsh 的搬砖/舵手 preset 已替换为 zcode 原生能力：

- **搬砖模式** → zcode 默认模式（全工具栈直接干活）
- **舵手模式** → zcode `orchestration` skill + `Agent` subagent（分解任务、委派、质量门禁）
- 多角色 pipeline → `damselfish/pipeline.py`（evaluator→developer→reviewer→tester）

## 搜索

SearXNG 运行在 Helm 自带的 venv 中（`searxng/`，submodule 源码 + 自带 `.venv`），通过 JSON API 搜索：

```bash
curl -s "http://127.0.0.1:8888/search?q=python+asyncio&format=json" | python3 -m json.tool
```

zcode agent 可直接用 Bash + curl 调用 SearXNG，或通过 WebFetch 搜索。

## 已归档

`deprecated/` 目录包含旧的 dsh 配置（dsh-config/、dsh-integration/），仅供参考。
