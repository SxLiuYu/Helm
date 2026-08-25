# Helm

> **Helm** = zcode（舵手）+ damselfish（领航）+ SearXNG（瞭望）
>
> 借鉴英伟达 AVO（Agentic Variation Operators）的 Harness 工程思想：优秀的系统架构能极大释放模型潜力——AVO 把 Claude Opus 5 的能力从 30% 提升到 100%。

## 命名

Helm，舵手。zcode 当舵手（监督 + 工具 + 循环），damselfish 选最快的船（智能路由多模型），SearXNG 做瞭望（搜索）。

## 架构

```
                          ┌─────────────────────────────────────┐
                          │         zcode (Helm 舵手)             │
                          │  Bash/Edit/Read/Grep/Glob skills     │
                          │  Agent subagents (并行/编排)          │
                          │  200+ skills (orchestration 等)       │
                          └────────────────┬────────────────────┘
                                           │ default provider
                                           ▼
                          ┌─────────────────────────────────────┐
                          │      damselfish (领航, 8086)          │
                          │  场景+人物+延迟+失败率 综合评分       │
                          │  三阶段回退: 串行→竞速→兜底          │
                          │  SQLite 记忆 · 熔断 · 健康检查       │
                          └────────────────┬────────────────────┘
                                           │ 智能路由
              ┌──────────────┬─────────────┼─────────────┬──────────────┐
              ▼              ▼             ▼             ▼              ▼
         agnes (4)      finna (5)    stepfun (5)   image/video(7)   ...

          SearXNG (8888, in-Helm venv) ← zcode agent 用 curl/Bash 调 JSON API 搜索
```

## AVO 四思想 → Helm 对应

| AVO 思想 | Helm 里对应什么 |
|---|---|
| **分层监督**（CEO 实时纠偏 + 高层决策） | zcode 的 Agent subagent + orchestration skill = 舵手；damselfish 选模型 = 选航向 |
| **记忆管理**（短期 + 长期） | damselfish SQLite（decisions/memory_events/sessions/projects）+ zcode memory |
| **迭代反馈循环**（方案→执行→反馈→修正） | damselfish 三阶段回退（串行→竞速→兜底）+ 熔断恢复 + zcode tool loop |
| **工具与环境集成**（丰富工具箱 + 统一抽象） | zcode 的 Bash/Edit/Read/Grep/Glob + 200+ skills + damselfish 的 21 模型池 |

## 仓库结构

```
Helm/
├── damselfish/              # 路由器本体（Python）
│   ├── damselfish/          # 源码：router/selector/config/app/store/pipeline
│   ├── scripts/
│   │   ├── start.sh         # 本地启动（source .env + uv run + PID + 日志）
│   │   └── stop.sh          # 停止（按 8086 端口, macOS lsof / Windows netstat）
│   ├── config.yml           # 路由配置 + 21 个 target（gitignore 不入库）
│   └── .env                 # API keys（gitignore 不入库）
├── searxng/                 # SearXNG 搜索引擎（自包含）
│   ├── src/                 # 引擎源码（git submodule, pin searxng@9fea412）
│   ├── settings.yml         # 完整配置（端口 8888、json、zh-CN、中国引擎）
│   ├── wincompat/pwd.py     # Windows 兼容 shim（Unix-only `pwd` 导入）
│   ├── start.sh / stop.sh   # 跨平台启停（PID+kill-0, 不依赖 lsof）
│   ├── .venv/               # Python 3.12 venv（src/requirements.txt, gitignore）
│   └── data/                # PID + 日志（gitignore）
├── scripts/                 # 全局脚本
│   ├── start.sh / stop.sh   # damselfish 启停
│   ├── safe-restart.sh      # 安全重启
│   ├── stability-report.sh  # 稳定性报告
│   └── watchdog.sh          # 看门狗
├── deprecated/              # 旧 dsh 配置（已归档，仅供参考）
│   ├── dsh-config/          # dsh settings/presets/plugins
│   └── dsh-integration/     # dsh 集成文档
├── tests/                   # pytest 测试
├── CLAUDE.md                # zcode 项目指令
├── README-LOCAL.md          # 本地部署细节
└── README.md                # 本文件
```

## 模型池（21 个 target）

**14 个 chat 类（参与路由）：**
- agnes：agnes-2.5-flash / agnes-2.0-flash（apihub + api.agnes-ai.cn 两端点，共 4 个）
- finna：glm-5.2、deepseek-v4-flash、deepseek-v4-pro、qwen3.6-plus、minimax-m2.7
- stepfun：step-3.7-flash、step-router-v1、stepaudio-2.5-chat、step-3.5-flash-2603、step-3.5-flash

**7 个 image/video（在池不参与 chat 路由）：**
- agnes-image-2.0-flash / 2.1-flash（+cn）、agnes-video-2.5 / v2.0（+cn）

## 快速部署

核心三步：

1. **启动 damselfish**：`bash scripts/start.sh`（8086）— Windows 开机自启：`wscript scripts/autostart.vbs install`
2. **启动 SearXNG**：`bash searxng/start.sh`（8888, submodule+venv，详见 `searxng/README.md`）
> 一键启动两者：`bash scripts/start-all.sh`（幂等）。Windows 开机自启：`wscript scripts/autostart.vbs install`（注册 HKCU Run key，详见 `DEPLOY-WINDOWS.md`）。
3. **配置 zcode**：在 `~/.zcode/v2/config.json` 添加 damselfish provider，`setting.json` 切换 `providerFamilyDomain`

详见 `README-LOCAL.md` 和 `CLAUDE.md`。

## 安全

- `.env` / `config.yml` / `data/` 全部在 `.gitignore`，密钥不入库
- damselfish 对 chat 请求做 API key 校验（`Authorization: Bearer`）
- SearXNG 绑定 127.0.0.1，不对外暴露

## 状态

- damselfish 路由器：完成，21 target 全 available，三阶段回退验证通过
- zcode 集成：完成，default=damselfish，端到端验证通过
- SearXNG：in-Helm venv（submodule + 自带 .venv）部署完成，JSON API 可用
- 旧 dsh 配置：已归档到 `deprecated/`

## 致谢

- damselfish 路由器：基于作者 sxliuyu 原项目
- zcode：GLM 驱动的 agent harness
- SearXNG：开源元搜索引擎
- AVO 设计思想：NVIDIA《AVO: Agentic Variation Operators for Autonomous Evolutionary Search》
