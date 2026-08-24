# SearXNG 配置 (Helm web research)

本目录包含 Helm 使用的 SearXNG 配置，部署时配合 `C:\SearXNG` 使用。

## 文件说明

| 文件 | 作用 |
|---|---|
| `settings.yml` | SearXNG 关键配置（server/search/启用引擎），精简版 |
| `start.sh` | 启动 SearXNG（端口 8888） |
| `stop.sh` | 停止 SearXNG |

## 部署

```bash
# SearXNG 部署在 C:\SearXNG (Python 3.12 venv)
# settings.yml 是精简版(只含关键配置), 实际运行用完整版
# 启停脚本在 C:\SearXNG\scripts\

bash /c/SearXNG/scripts/start.sh   # 启动(端口 8888)
bash /c/SearXNG/scripts/stop.sh    # 停止
```

## 引擎

启用的搜索引擎（能连通的）：
- bing / bing images / bing news / bing videos
- baidu / baidu images
- 360search
- sogou

google/duckduckgo/brave/wikipedia/startpage 已禁用（国内连不通）。

## DSH 集成

agent 通过 `web-research` skill（`~/.agents/skills/web-research/`）用 curl 调 SearXNG JSON API：
```
curl -s "http://127.0.0.1:8888/search?q=QUERY&format=json"
```

## 免登录

SearXNG 的 admin API 对 localhost 免认证（damselfish dashboard 嵌入用）。
