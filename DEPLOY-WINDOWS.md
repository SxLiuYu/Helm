# Helm 项目 Windows 部署指南

本指南说明如何在 Windows 设备上部署 Helm 项目（`zcode` + `damselfish` + `SearXNG`），并从 GitHub 拉取代码。

Git 远程地址：`ssh://git@ssh.github.com:443/SxLiuYu/Helm.git`

---

## 前置条件

| 依赖 | 说明 | 安装方式 |
|---|---|---|
| Python 3.11+（推荐 3.14） | damselfish 运行所需 | [python.org](https://www.python.org/downloads/windows/) |
| `uv` | Python 包管理器 | `pip install uv` 或 `winget install astral-sh.uv` |
| Git | 克隆仓库 | [git-scm.com](https://git-scm.com/download/win) |
| Docker Desktop | 运行 SearXNG（推荐方案） | [docker.com](https://www.docker.com/products/docker-desktop/) |
| zcode 桌面客户端 | Agent 界面，从 [z.ai](https://z.ai) 下载 | — |

> **可选方案**：SearXNG 也可以不通过 Docker，改用 Python venv 运行（见第 4 步）。

---

## 步骤

### 1. 克隆仓库

```bash
git clone ssh://git@ssh.github.com:443/SxLiuYu/Helm.git
cd Helm
```

> **说明**：需要提前配置好 SSH 密钥并将公钥添加到 GitHub 账户。

---

### 2. 配置 damselfish

#### 2.1 创建 `config.yml`

`config.yml` 位于 `.gitignore` 中，不会随仓库拉取，需手动创建：

```bash
copy config.example.yml config.yml
```

用文本编辑器打开 `config.yml`，确认以下内容无需额外修改（默认值通常已就绪）：

- 服务监听端口：`8086`
- `targets` 段：已预配置各场景和 persona 对应的 Provider/Model 路由

#### 2.2 创建 `.env`

同样从示例文件复制并填写所有 API Key：

```bash
copy .env.example .env
```

使用文本编辑器打开 `.env`，填写以下变量：

| 变量 | 说明 |
|---|---|
| `AGNES_API_KEY` | Agnes API 密钥 |
| `AGNES_CN_API_KEY` | Agnes 中国节点 API 密钥 |
| `FINNA_GLM52_API_KEY` | Finna GLM-5.2 模型密钥 |
| `FINNA_DEEPSEEK_FLASH_API_KEY` | Finna DeepSeek V4 Flash 密钥 |
| `FINNA_DEEPSEEK_PRO_API_KEY` | Finna DeepSeek V4 Pro 密钥 |
| `FINNA_QWEN_API_KEY` | Finna Qwen3.6 Plus 密钥 |
| `FINNA_MINIMAX_API_KEY` | Finna MiniMax M2.7 密钥 |
| `STEPFUN_API_KEY_01` ~ `STEPFUN_API_KEY_10` | StepFun 共 10 个密钥 |
| `DAMSELFISH_API_KEY` | damselfish 内部 API Key，两台设备可共用同一值 |

> **安全提醒**：`.env` 和 `config.yml` 均在 `.gitignore` 中，切勿手动提交。

---

### 3. 安装依赖并启动 damselfish

```bash
# 同步依赖（--extra office 一并装 office-files skill 依赖：python-docx/openpyxl/pdfplumber 等；
# 不加 --extra office，下次 uv sync 会把这些库清掉）
uv sync --extra office

# 启动 damselfish（Git Bash / WSL）
bash scripts/start.sh
```

#### Windows 原生启动（PowerShell / CMD）

如果 `scripts/start.sh` 的 `lsof` 检测在 Windows 上表现不佳，可直接启动：

```bash
# Git Bash
set -a
source .env
set +a
uv run damselfish --config config.yml
```

或创建 `start.bat`：

```batch
@echo off
set -a
call :source_env
set +a
uv run damselfish --config config.yml
goto :eof

:source_env
call .env
exit /b 0
```

#### 开机自启（可选）

使用 **Windows Task Scheduler** 或 [NSSM](https://nssm.cc/) 将 damselfish 注册为系统服务：

```
程序：C:\Users\<用户>\AppData\Local\Microsoft\WindowsApps\bash.exe
参数：-c "cd /c/Users/<用户>/Helm && set -a && source .env && set +a && uv run damselfish --config config.yml"
起始位置：C:\Users\<用户>\Helm
```

---

### 4. 启动 SearXNG

SearXNG 引擎源码作为 git submodule 放在 `searxng/src/`（真正的搜索引擎没发布到 PyPI，只能从 git 源码跑；详见 `searxng/README.md`）。

#### 方案 A：Python venv + submodule（本机已用，推荐）

```bash
# 4.1 拉取 submodule（引擎源码，101MB）
git submodule update --init searxng/src
# Windows 若报含 ':' 的模板文件无法 checkout：
#   git -c core.protectNTFS=false submodule update --init searxng/src

# 4.2 建 venv 装依赖（pypi.org 国内连不上，用清华镜像）
uv venv searxng/.venv --python 3.12
uv pip install -r searxng/src/requirements.txt \
  --python searxng/.venv/Scripts/python.exe \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple
uv pip install tzdata --python searxng/.venv/Scripts/python.exe \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple   # 修 Asia/Shanghai zoneinfo 报错

# 4.3 启停
bash searxng/start.sh   # 端口 8888
bash searxng/stop.sh
```

> Windows 兼容：`searxng/wincompat/pwd.py` 是 shim，满足上游 `searx/valkeydb.py` 对 `pwd`（Unix-only）的导入；`start.sh` 用 `cygpath` 把路径转 Windows 格式、PID-file + `kill -0` 守卫，不依赖 `lsof`。

#### 方案 B：Docker（可选，本机未装 Docker）

```bash
docker run -d --name searxng-hermes -p 8888:8080 -v "%cd%/searxng/settings.yml:/etc/searxng/settings.yml:ro" --restart unless-stopped searxng/searxng:latest
```

> `searxng/settings.yml` 是完整配置（含 `secret_key`、json 格式、中国引擎、`default_doi_resolver`），Docker 与 venv 两种方式都用它。

---

### 5. 配置 zcode

#### 5.1 添加 damselfish Provider

打开（或创建）`%APPDATA%\zcode\v2\config.json`，添加以下 Provider 条目：

```json
{
  "13f8b742-7dad-4959-a062-8fed81c8d907": {
    "name": "damselfish",
    "kind": "openai-compatible",
    "options": {
      "apiKey": "df-40b09bd9ab5fed3a7375cb8c",
      "baseURL": "http://127.0.0.1:8086/v1",
      "apiKeyRequired": true
    },
    "source": "custom",
    "models": {
      "damselfish/auto": {
        "limit": {
          "context": 200000
        },
        "modalities": {
          "input": ["text"],
          "output": ["text"]
        },
        "zcode": {
          "modified": true
        }
      }
    }
  }
}
```

> `apiKey` 字段值为 damselfish 的接口密钥，与 `.env` 中的 `DAMSELFISH_API_KEY` 保持一致。

#### 5.2 切换默认 Provider

打开（或创建）`%APPDATA%\zcode\v2\setting.json`，设置：

```json
{
  "providerFamilyDomain": "damselfish",
  "modelProviderFamilySelectedKeys": {
    "damselfish": "13f8b742-7dad-4959-a062-8fed81c8d907"
  }
}
```

#### 5.3 重启 zcode

保存两份配置文件后，完全退出 zcode 并重新启动，使配置生效。

---

### 6. 验证部署

在 Git Bash 或 PowerShell 中执行：

```bash
# damselfish 健康检查
curl http://127.0.0.1:8086/health

# damselfish 模型列表
curl -H "Authorization: Bearer df-40b09bd9ab5fed3a7375cb8c" http://127.0.0.1:8086/v1/models

# SearXNG 搜索测试
curl "http://127.0.0.1:8888/search?q=test&format=json"
```

三项均返回 200 且含有效数据即表示部署成功。

---

## 注意事项

- `.env` 和 `config.yml` 已加入 `.gitignore`，从 GitHub 拉取后**必须手动创建**，否则 damselfish 无法启动。
- damselfish 的 API Key（`DAMSELFISH_API_KEY`）是自定义密钥，两台设备（macOS + Windows）应使用**相同的值**，确保 zcode 认证通过。
- SearXNG 的 `secret_key` 已写入 `searxng/settings.yml` 并随仓库一起维护，Windows 端无需额外配置。
- Windows 上建议使用 **Git Bash** 运行 shell 脚本，以避免 Bash 语法兼容问题。
- 如需开机自启 damselfish，可使用 **Windows Task Scheduler** 或 [NSSM](https://nssm.cc/)。
- 确保 `127.0.0.1:8086`（damselfish）和 `127.0.0.1:8888`（SearXNG）两个端口未被其他程序占用。
- 如遇到 Windows Defender / 防火墙拦截，请为 `uv.exe`、`python.exe` 和 `bash.exe` 放行。
