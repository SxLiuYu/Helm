# Damselfish 本地部署 + zcode 集成

## 架构

```
zcode (harness) ──default model──> damselfish (127.0.0.1:8086) ──智能路由──> 多个免费上游
```

zcode 的 default provider 设为 `damselfish`，所有 agent 请求先到本地 damselfish，由 damselfish 按 **场景(scenario) + 人物(persona) + 实时延迟 + 失败率** 综合评分选最优上游，并在 429/超时/503 时三阶段回退（串行→并行竞速→串行兜底）。

## 模型池（21 个 target）

**14 个 chat 类（参与路由）：**
- agnes：agnes-2.5-flash / agnes-2.0-flash（apihub + api.agnes-ai.cn 两端点，共 4 个）
- finna：glm-5.2、deepseek-v4-flash、deepseek-v4-pro、qwen3.6-plus、minimax-m2.7
- stepfun：step-3.7-flash、step-router-v1、stepaudio-2.5-chat、step-3.5-flash-2603、step-3.5-flash

**7 个 image/video（记录在池，不参与 chat 路由）：**
- agnes-image-2.0-flash / 2.1-flash（+cn）、agnes-video-2.5 / v2.0（+cn）
- 注：damselfish 当前是 chat completions router，不调 image/video endpoint。

## 启停

```bash
bash scripts/start.sh    # 后台启动, PID 写 data/damselfish.pid, 日志 data/damselfish.log
bash scripts/stop.sh     # 按 8086 端口找 listener (macOS lsof / Windows netstat)
```

start.sh 会 `source .env` 注入 key，并检测 8086 端口避免重复启动。

macOS 上 damselfish 由 launchd 管理 (`com.damselfish.local`)，开机自动启动，通常不需要手动操作。

## 配置文件

| 文件 | 作用 |
|---|---|
| `config.yml` | 路由参数 + scenarios/personas + targets 列表（21 个模型） |
| `.env` | API keys + DAMSELFISH_API_KEY，**gitignore 不入库** |
| `data/managed-nodes.json` | admin 页面动态节点（当前清空，全用 config.yml targets） |
| `data/damselfish.db` | SQLite 指标/路由决策/跨模型会话记忆 |

⚠️ `.env` 和 `config.yml` 都在 `.gitignore`（含密钥/本地路径），不会进 git。

## zcode 集成（~/.zcode/v2/）

- `config.json` → provider `damselfish`（UUID `13f8b742-7dad-4959-a062-8fed81c8d907`）：kind `openai-compatible`，baseURL `http://127.0.0.1:8086/v1`，model `damselfish/auto`
- `setting.json` → `providerFamilyDomain` = damselfish UUID
- zcode 调 damselfish 时带 `Authorization: Bearer df-40b09bd9ab5fed3a7375cb8c`
- zcode 不发送 scenario/persona 头，damselfish 从消息内容自动推断

## SearXNG 搜索

SearXNG 运行在 Docker 容器 `searxng-hermes` 中，端口 8888：

```bash
docker start searxng-hermes   # 启动
docker stop searxng-hermes    # 停止
curl -s "http://127.0.0.1:8888/search?q=test&format=json" | python3 -m json.tool  # 验证
```

## 注意

- **damselfish 必须常驻**：zcode default=damselfish，damselfish 断了全失败。launchd 管理开机自启。
- damselfish 不自动 load .env，必须经 start.sh（或 `set -a; source .env; set +a`）启动。
- image/video 模型免费但 damselfish 当前不路由（chat router 限制）。
- 旧 dsh 配置已归档到 `deprecated/` 目录。
