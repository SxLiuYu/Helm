# DSH 集成

把 damselfish 接入 DeepSeek Harness (DSH) 作为 default LLM provider。

## 架构

```
DSH agent (3080) ──default──> damselfish (127.0.0.1:8086) ──智能路由──> 21 个免费上游
```

DSH 的 supervisor/subagent（舵手）编排工具与监督循环，damselfish（领航）按场景/人物/延迟/失败率选最优上游并三阶段回退。

## 接入步骤

1. **启动 damselfish**（见上级 README-LOCAL.md 的启停）

2. **加 provider 到 DSH settings**：把 `damselfish.provider.yml` 内容 merge 进 `~/.dsh/settings.yaml` 的 `llm-pi-ai.providers` 下

3. **加 key 到 DSH credentials**：从 `Helm/.env` 取 `DAMSELFISH_API_KEY` 的值，作为 `DAMSELFISH_API_KEY` 追加进 `~/.dsh/.credentials.yaml`

4. **设 default**（可选，全量走 damselfish）：`~/.dsh/settings.yaml`
   ```yaml
   agent-default-model:
     provider: damselfish
     model: damselfish/auto
   ```

5. **重启 DSH**：`powershell.exe -ExecutionPolicy Bypass -File ~/.dsh/restart-dsh-web.ps1`

6. **开机自启 damselfish**：把 `autostart.bat` 复制到启动文件夹
   `C:\Users\<user>\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\`

## 注意

- damselfish 必须常驻：default=damselfish 时 damselfish 断了 DSH 全失败
- DSH 调 damselfish 带 `Authorization: Bearer <DAMSELFISH_API_KEY>`，damselfish 用 HMAC 校验
- 改 DSH 配置后要重启 DSH web 才生效
