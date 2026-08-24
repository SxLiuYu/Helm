# Helm

> **Helm** = DeepSeek Harness（舵手）+ damselfish（领航）
>
> 借鉴英伟达 AVO（Agentic Variation Operators）的 Harness 工程思想：优秀的系统架构能极大释放模型潜力——AVO 把 Claude Opus 5 的能力从 30% 提升到 100%。

## 命名

Helm，舵手。DSH 当舵手（监督 + 工具 + 循环），damselfish 选最快的船（智能路由多模型）。

## 架构

```
                          ┌─────────────────────────────────────┐
                          │           DSH (Helm 舵手)            │
                          │  3080 web · supervisor/subagent     │
                          │  ctx.skills (43 Matt Pocock skills) │
                          │  ctx.shell/fs/web/subprocess        │
                          │  ctx.workflow · persona · goal      │
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
```

## AVO 四思想 → Helm 对应

| AVO 思想 | Helm 里对应什么 |
|---|---|
| **分层监督**（CEO 实时纠偏 + 高层决策） | DSH 的 supervisor/subagent（liangshen preset 两阶段 anchor + 工具编排）= 舵手；damselfish 选模型 = 选航向 |
| **记忆管理**（短期 + 长期） | damselfish SQLite（decisions/memory_events/sessions/projects）+ DSH session/compaction |
| **迭代反馈循环**（方案→执行→反馈→修正） | damselfish 三阶段回退（串行→竞速→兜底）+ 熔断恢复 + DSH tool loop |
| **工具与环境集成**（丰富工具箱 + 统一抽象） | DSH 的 ctx.skills / ctx.shell / ctx.fs / ctx.web / ctx.subprocess + damselfish 的 21 模型池 |

## 仓库结构

```
Helm/
├── damselfish/              # 路由器本体（Python，从千机迁移来）
│   ├── damselfish/          # 源码：router/selector/config/app/store
│   ├── scripts/
│   │   ├── start.sh         # 本地启动（source .env + uv run + PID + 日志）
│   │   └── stop.sh          # 停止（按 8086 端口 taskkill /T 杀树）
│   ├── config.yml           # 路由配置 + 21 个 target（gitignore 不入库）
│   ├── .env                 # 19 个 API key（gitignore 不入库）
│   └── README-LOCAL.md      # damselfish 本地部署细节
├── dsh-integration/         # DSH 集成配置
│   ├── damselfish.provider.yml    # 要 merge 进 ~/.dsh/settings.yaml 的 provider 段
│   ├── damselfish.credentials.yaml # 要追加进 ~/.dsh/.credentials.yaml 的 key
│   ├── autostart.bat        # 开机自启（复制到 Startup 文件夹）
│   └── README.md            # DSH 接入步骤
└── README.md                # 本文件（Helm 总览）
```

## 模型池（21 个 target）

**14 个 chat 类（参与路由）：**
- agnes：agnes-2.5-flash / agnes-2.0-flash（apihub + api.agnes-ai.cn 两端点，共 4 个）
- finna：glm-5.2、deepseek-v4-flash、deepseek-v4-pro、qwen3.6-plus、minimax-m2.7
- stepfun：step-3.7-flash、step-router-v1、stepaudio-2.5-chat、step-3.5-flash-2603、step-3.5-flash

**7 个 image/video（在池不参与 chat 路由）：**
- agnes-image-2.0-flash / 2.1-flash（+cn）、agnes-video-2.5 / v2.0（+cn）
- 注：damselfish 当前是 chat completions router，不调 image/video endpoint；要真正用需扩展

## 快速部署

见 `dsh-integration/README.md` 和 `damselfish/README-LOCAL.md`。核心三步：

1. **启动 damselfish**：`bash damselfish/scripts/start.sh`（8086）
2. **接入 DSH**：merge `dsh-integration/damselfish.provider.yml` → `~/.dsh/settings.yaml`，追加 key → `~/.dsh/.credentials.yaml`，重启 DSH
3. **开机自启**：复制 `dsh-integration/autostart.bat` 到启动文件夹

## 安全

- `.env` / `config.yml` / `managed-nodes.json` / `data/` 全部在 `.gitignore`，密钥不入库
- damselfish 对 chat 请求做 HMAC key 校验（`Authorization: Bearer`）
- DSH 有 `dsh-sandbox` + `fs/* policy hooks`（读不可信内容→执行敏感动作之间可加策略层）

## 状态

- damselfish 路由器：完成，21 target 全 available，三阶段回退验证通过
- DSH 集成：完成，default=damselfish，端到端验证通过（DSH → damselfish → 上游路由 + 503 回退）
- 跨设备记忆：不做（两设备 memory db 各自独立）

## 致谢

- damselfish（千机）路由器：基于作者 sxliuyu 原项目
- DeepSeek Harness (DSH)：DeepSeek 官方 agent runtime
- AVO 设计思想：NVIDIA《AVO: Agentic Variation Operators for Autonomous Evolutionary Search》
