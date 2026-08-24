# Damselfish 本地部署 + DSH 集成

## 架构

```
DSH (agent, 3080) ──default model──> damselfish (127.0.0.1:8086) ──智能路由──> 多个免费上游
```

DSH 的 default provider 设为 `damselfish`，所有 agent 请求先到本地 damselfish，由 damselfish 按 **场景(scenario) + 人物(persona) + 实时延迟 + 失败率** 综合评分选最优上游，并在 429/超时/503 时三阶段回退（串行→并行竞速→串行兜底）。

## 模型池（21 个 target）

**14 个 chat 类（参与路由）：**
- agnes：agnes-2.5-flash / agnes-2.0-flash（apihub + api.agnes-ai.cn 两端点，共 4 个）
- finna：glm-5.2、deepseek-v4-flash、deepseek-v4-pro、qwen3.6-plus、minimax-m2.7
- stepfun：step-3.7-flash、step-router-v1、stepaudio-2.5-chat、step-3.5-flash-2603、step-3.5-flash

**7 个 image/video（记录在池，不参与 chat 路由）：**
- agnes-image-2.0-flash / 2.1-flash（+cn）、agnes-video-2.5 / v2.0（+cn）
- 注：damselfish 当前是 chat completions router，不调 image/video endpoint；要真正用这些免费图像/视频模型需扩展 damselfish 或直接调 agnes API。

## 启停

```bash
bash scripts/start.sh    # 后台启动, PID 写 data/damselfish.pid, 日志 data/damselfish.log
bash scripts/stop.sh     # 按 8086 端口找 listener, taskkill /T 杀整棵进程树
```

start.sh 会 `source .env` 注入 key（damselfish 不自动 load .env，靠 systemd EnvironmentFile 或手动 source），并检测 8086 端口避免重复启动。

## 开机自启

`damselfish-autostart.bat` 已放在启动文件夹：
`C:\Users\mi\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\`

登录时 Windows 自动跑它 → Git Bash 跑 start.sh → damselfish 常驻。

## 配置文件

| 文件 | 作用 |
|---|---|
| `config.yml` | 路由参数 + scenarios/personas + targets 列表（21 个模型） |
| `.env` | 19 个 API key（finna 5 + agnes 2 + stepfun 10 + DAMSELFISH_API_KEY + DEVICE_ID），**gitignore 不入库** |
| `data/managed-nodes.json` | admin 页面动态节点（当前清空，全用 config.yml targets） |
| `data/damselfish.db` | SQLite 指标/路由决策/跨模型会话记忆 |

⚠️ `.env` 和 `config.yml` 都在 `.gitignore`（含密钥/本地路径），不会进 git。

## DSH 集成（~/.dsh/）

- `settings.yaml` → `llm-pi-ai.providers.damselfish`：baseURL `http://127.0.0.1:8086/v1`，model `damselfish/auto`，apiKeyEnv `DAMSELFISH_API_KEY`
- `.credentials.yaml` → `DAMSELFISH_API_KEY`（值同 damselfish/.env）
- `agent-default-model` → `provider: damselfish, model: damselfish/auto`
- DSH 调 damselfish 时带 `Authorization: Bearer <DAMSELFISH_API_KEY>`，damselfish 用它做 HMAC 校验

改 DSH 配置后跑 `restart-dsh-web.ps1` 重启 DSH web（3080）生效。

## 注意

- **damselfish 必须常驻**：default=damselfish，damselfish 断了 DSH 全失败。开机自启 bat 解决重启后自启。
- damselfish 不自动 load .env，必须经 start.sh（或 `set -a; source .env; set +a`）启动。
- image/video 模型免费但 damselfish 当前不路由（chat router 限制）。
- agnes-pro 是收费模型，已排除。
