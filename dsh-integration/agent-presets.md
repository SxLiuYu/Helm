# Agent Presets

Helm 三个可切换 preset（在 DSH web 底部按钮切换）：

| preset | 用途 | persona |
|---|---|---|
| minimal | 轻问答/快速测试 | helpful assistant, bash + editor only |
| liangshen | 强开发(默认) | 梁神模式, 两阶段 anchor + PTC + 全工具 |
| helmsman | CEO 多角色办公 | 监督者, 分解/委派/门禁, 不直接写代码 |

helmsman preset 位置: `~/.dsh/.agent-presets/helmsman/`
- preset.yml: 舵手模式, order 5
- agent.cordis.yml: persona=CEO supervisor, delegation 组(subagent spawn/fork + ralph + workflow)
- custom-bash.mjs + tool-bootstrap.mjs: 复制自 liangshen

切换: DSH web 底部点击 preset 名 → 选 helmsman(舵手模式)
