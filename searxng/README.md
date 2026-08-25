# SearXNG (Helm web research, 端口 8888)

本目录是 Helm 自包含的 SearXNG：引擎源码以 git submodule 形式放在 `src/`，运行用 `searxng/.venv`，配置用本目录的 `settings.yml`。**不再依赖外部的 `C:\SearXNG`。**

> 真正的 SearXNG 搜索引擎没发布到 PyPI（PyPI 上的 `searxng` 包是个无关的 MCP 客户端），所以只能从 git 源码跑——这就是用 submodule 而非 `pip install` 的原因。

## 文件

| 路径 | 作用 |
|---|---|
| `src/` | SearXNG 引擎源码（git submodule，pin 到上游 commit `9fea412`） |
| `settings.yml` | 完整配置（端口 8888、json 格式、zh-CN、中国引擎、Helm `secret_key`、`default_doi_resolver`） |
| `wincompat/pwd.py` | Windows 兼容 shim：上游 `searx/valkeydb.py` 无条件 `import pwd`（Unix-only），本 shim 放在 `searx` 源码之前以满足导入 |
| `start.sh` / `stop.sh` | 跨平台启停（PID-file + `kill -0`，不依赖 `lsof`） |
| `.venv/` | Python 3.12 venv，依赖来自 `src/requirements.txt`（已 gitignore） |
| `data/` | PID + 日志（已 gitignore） |

## 初次部署

```bash
# 1. 拉取 submodule（引擎源码，101MB）
git submodule update --init searxng/src

# 2. 建 venv 装依赖（pypi.org 国内连不上，用清华镜像）
uv venv searxng/.venv --python 3.12
uv pip install -r searxng/src/requirements.txt \
  --python searxng/.venv/Scripts/python.exe \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple
uv pip install tzdata --python searxng/.venv/Scripts/python.exe \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple   # 修 Asia/Shanghai zoneinfo 报错
```

> Windows 首次 `git submodule update --init` 若因 NTFS 拒收含 `:` 的模板文件报错，加 `-c core.protectNTFS=false` 再跑（该文件是 Apache 模板，运行不需要）。

## 启停

```bash
bash searxng/start.sh    # 后台启动（nohup），写 searxng/data/searxng.pid
bash searxng/stop.sh     # 停止
```

## 验证

```bash
curl http://127.0.0.1:8888/healthz
curl "http://127.0.0.1:8888/search?q=test&format=json"
```

## 引擎

默认启用的中国引擎：`baidu` / `bing` / `360search` / `sogou` / `bilibili`。部分引擎（google cse、wikidata）国内会超时，搜索总时延约 10–20s（不影响有结果的引擎）。可在 `settings.yml` 里把不用的引擎设 `enabled: false` 以加速。

## 升级引擎源码

```bash
cd searxng/src && git checkout <新commit> && cd -
git submodule sync
# 若上游改了 requirements.txt，重装：uv pip install -r searxng/src/requirements.txt --python searxng/.venv/Scripts/python.exe --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

## agent 集成

`web-research` skill 用 curl 调 SearXNG JSON API：

```bash
curl -s "http://127.0.0.1:8888/search?q=QUERY&format=json"
```
