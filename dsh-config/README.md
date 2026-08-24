# DSH 配置 (Helm 集成)

本目录包含 Helm 使用的 DSH (DeepSeek Harness) 配置文件，部署时复制到 `~/.dsh/`。

## 文件说明

| 文件 | 作用 | 部署位置 |
|---|---|---|
| `settings.yaml` | DSH 主配置（provider/preset/sidebar） | `~/.dsh/settings.yaml` |
| `cordis.patch.yml` | web profile 插件开关 | `~/.dsh/profiles/web/cordis.patch.yml` |
| `restart-dsh-web.ps1` | DSH web 重启脚本（智能检测：settings 热加载 vs cordis 需重启） | `~/.dsh/restart-dsh-web.ps1` |
| `presets/banzhuan/` | 搬砖模式 preset（日常开发，全工具栈） | `~/.dsh/.agent-presets/banzhuan/` |
| `presets/helmsman/` | 舵手模式 preset（CEO 监督者） | `~/.dsh/.agent-presets/helmsman/` |
| `plugins/damselfish-link/` | DSH 左上角 Damselfish 跳转链接插件 | `~/.dsh/plugins/damselfish-link/` |

## Presets

| preset | 模式 | 用途 |
|---|---|---|
| banzhuan (搬砖模式) | 默认 | 日常代码开发，全工具栈直接干活 |
| helmsman (舵手模式) | 可选 | CEO 监督者，分解/委派/门禁 |

## 部署

```bash
# settings + cordis + restart 脚本
cp dsh-config/settings.yaml ~/.dsh/settings.yaml
cp dsh-config/cordis.patch.yml ~/.dsh/profiles/web/cordis.patch.yml
cp dsh-config/restart-dsh-web.ps1 ~/.dsh/restart-dsh-web.ps1

# presets
cp -r dsh-config/presets/banzhuan ~/.dsh/.agent-presets/
cp -r dsh-config/presets/helmsman ~/.dsh/.agent-presets/

# damselfish-link 插件
cp -r dsh-config/plugins/damselfish-link ~/.dsh/plugins/

# 注意: .credentials.yaml 含 API key, 不在此 repo, 需手动配置
```

## 注意

- `.credentials.yaml`（含 DAMSELFISH_API_KEY 等）不在 repo，需手动创建
- DSH web 前端 `index.html` 的 Damselfish 链接注入是直接改 DSH 包文件，升级会覆盖
- `promotedPresentation: native`（banzhuan + helmsman 都改成 native，避免 run_code 报错）
