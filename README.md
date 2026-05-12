<div align="center">

# Rapid Inbox

**本地优先的临时邮箱服务**

内置 SMTP 监听器、公开收件箱、管理后台和 HTTP API<br/>
邮件、附件、元数据和审计全部落本地磁盘与 SQLite

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.136-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](CHANGELOG.md)

[快速开始](#快速开始) · [特性](#特性) · [配置](#配置) · [使用](#基本使用) · [贡献](CONTRIBUTING.md) · [安全](SECURITY.md)

</div>

---

## 简介

Rapid Inbox 是一个本地优先的临时收件服务，面向 **验证码收件**、**测试环境邮件捕获**、**内部工具联调** 和 **轻量自托管** 场景。

核心目标是把 **收得到、看得清、管得住、容易恢复** 做好，不依赖外部邮件服务或云端数据库。

> 项目目前处于早期版本（Alpha），接口和数据结构可能继续调整。

## 特性

| 分类 | 能力 |
| --- | --- |
| **邮件接收** | 内置 SMTP 监听器，可与 HTTP 同进程启动，也可独立为单独进程 |
| **收件箱** | 公开邮箱页面支持邮件列表、详情、原始 EML、HTML 预览和附件下载 |
| **实时更新** | 公开收件箱通过 WebSocket 推送，管理后台通过 SSE 查看 SMTP 接收事件 |
| **验证码识别** | 打分制提取算法，支持中英日韩西多语言上下文与字母数字/分隔符组合 |
| **管理后台** | 域名管理、DNS 检查、邮箱管理、邮件重解析、审计日志和系统设置 |
| **API Key** | 细粒度作用域、域名/邮箱模式、Header/Query、IP 白名单、限速、过期、吊销 |
| **持久化** | SQLite 保存索引元数据，磁盘保存 raw / text / html / attachments / manifests |
| **启动恢复** | 根据 manifest 自动修复缺失元数据，降低异常退出后的数据不一致风险 |
| **保留策略** | 邮件默认保留 10 分钟，后台任务自动清理过期记录和落盘文件 |
| **维护工具** | 管理后台可清空邮件数据、删除落盘文件并压缩 SQLite 数据库 |

## 技术栈

`Python 3.10+` · `FastAPI` · `aiosmtpd` · `Jinja2` · `SQLite` · `Uvicorn` · `WebSocket` · `SSE`

## 快速开始

```bash
# 1. 创建虚拟环境并安装
python3 -m venv .venv
.venv/bin/pip install -c constraints-dev.txt -e ".[dev]"

# 2. 准备环境变量
cp .env.example .env

# 3. 启动 HTTP + 内嵌 SMTP
.venv/bin/rapid-inbox-http
```

打开管理后台：

```text
http://127.0.0.1:8000/admin/login
```

默认管理员账号：

```text
用户名：admin
密码：change-me-now
```

> 首次 bootstrap 管理员登录后，后台会**强制**进入系统设置页修改初始密码；完成改密前不能访问其他后台页面。

默认启动器使用当前工作目录作为项目运行目录。从仓库根目录启动时，数据会写入：

```text
./storage/
./storage/app.db
```

> [!WARNING]
> 首次对外部署前，请务必修改 `.env` 中的管理员密码、API Token、公开 API Key 和监听地址。

## 启动方式

<details>
<summary><b>HTTP + 内嵌 SMTP 同进程</b>（推荐）</summary>

```bash
.venv/bin/rapid-inbox-http
```

</details>

<details>
<summary><b>仅启动 SMTP 监听器</b></summary>

```bash
.venv/bin/rapid-inbox-smtp
```

</details>

<details>
<summary><b>开发模式（模块入口）</b></summary>

```bash
.venv/bin/uvicorn app.main:app --reload
```

直接使用 `uvicorn app.main:app` 时**不会**启用内嵌 SMTP。需要接收 SMTP 邮件时，请使用 `rapid-inbox-http`，或另开进程运行 `rapid-inbox-smtp`。

</details>

<details>
<summary><b>C++ SMTP ingestd + Python HTTP</b>（高吞吐生产模式）</summary>

```bash
# 1. 启动 Python HTTP，不启用内嵌 SMTP
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2. 启动 C++ SMTP 收件入口
cmake -S cpp/ingestd -B cpp/ingestd/build
cmake --build cpp/ingestd/build
SMTP_HOST=0.0.0.0 SMTP_PORT=25 cpp/ingestd/build/rapid-inbox-ingestd --base-dir .
```

C++ ingestd 的 `250 queued` 表示邮件已进入内存队列；正常停止会 drain，异常崩溃或断电可能丢失尚未落盘的内存队列邮件。

</details>

## 配置

启动器读取变量的优先级：

```text
真实环境变量  >  当前工作目录下的 .env  >  app/config.py 默认值
```

<details>
<summary><b>完整环境变量表</b></summary>

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STORAGE_ROOT` | `./storage` | 邮件文件、附件和临时文件根目录 |
| `DATABASE_PATH` | `./storage/app.db` | SQLite 数据库路径 |
| `BOOTSTRAP_ADMIN_USERNAME` | `admin` | 首次启动自动创建的管理员用户名 |
| `BOOTSTRAP_ADMIN_PASSWORD` | `change-me-now` | 首次启动自动创建的管理员密码 |
| `SESSION_COOKIE_NAME` | `rapid_inbox_session` | 管理后台登录态 Cookie 名称 |
| `HOST` | `127.0.0.1` | HTTP 监听地址 |
| `PORT` | `8000` | HTTP 监听端口 |
| `SMTP_HOST` | `127.0.0.1` | SMTP 监听地址 |
| `SMTP_PORT` | `25` | SMTP 监听端口 |
| `MAX_MESSAGE_SIZE_BYTES` | `52428800` | 单封邮件最大体积 |
| `MAX_RECIPIENTS_PER_MESSAGE` | `20` | 单封邮件最大收件人数 |
| `SMTP_IDLE_TIMEOUT_SECONDS` | `30` | SMTP 会话空闲断开时间 |
| `SMTP_MAX_CONCURRENT_CONNECTIONS` | `0` | SMTP 并发连接上限，`0` 表示不限制 |
| `SMTP_CONNECTION_RATE_LIMIT_COUNT` | `0` | 每个 IP 在短窗口内允许建立的 SMTP 连接数，`0` 表示不限制 |
| `SMTP_CONNECTION_RATE_LIMIT_WINDOW_SECONDS` | `60` | SMTP per-IP 连接限流窗口 |
| `PARSE_WORKER_COUNT` | `4` | 后台 MIME 解析 worker 数量 |
| `FSYNC_STORAGE_WRITES` | `false` | 是否对邮件文件写入执行强制 fsync |
| `DISK_WARNING_THRESHOLD_PERCENT` | `85` | Dashboard 磁盘使用率告警阈值 |
| `ADMIN_TOKEN` | 未启用 | 兼容管理 API 的管理令牌；只有显式配置为非默认随机值时才启用 |
| `PUBLIC_API_KEY` | 未启用 | 兼容公开 API 的访问密钥；建议改用后台创建的 API Key |

</details>

## 基本使用

1. 启动服务并登录 `/admin/login`
2. 在管理后台添加可接收的根域名
3. 将测试邮件投递到任意匹配邮箱地址，例如 `code@example.com`
4. 在公开页面 `/mail/{mailbox_address}` 或管理后台查看邮件
5. 使用 API Key 为测试脚本、内部工具或自动化流程读取邮件

### 公开页面

```text
GET  /
GET  /mail/{mailbox_address}
GET  /mail/{mailbox_address}/{delivery_id}
GET  /mail/{mailbox_address}/{delivery_id}/raw
GET  /mail/{mailbox_address}/{delivery_id}/attachments/{attachment_id}
```

### 公开 API 示例

```bash
curl \
  -H "X-API-Key: <your-public-api-key>" \
  "http://127.0.0.1:8000/api/v1/public/mailboxes/code@example.com/messages"
```

支持 `limit`、兼容旧版的 `offset`，并返回 `next_cursor`。新集成建议使用 `next_cursor` 翻页：

```bash
curl \
  -H "X-API-Key: <your-public-api-key>" \
  "http://127.0.0.1:8000/api/v1/public/mailboxes/code@example.com/messages?limit=20&cursor=<next_cursor>"
```

## 数据与保留策略

Rapid Inbox 使用 SQLite 保存结构化数据，邮件内容拆分保存在本地目录：

```text
storage/
├── app.db           # SQLite 索引与元数据
├── raw/             # 原始 EML
├── text/            # 解析后的纯文本
├── html/            # 解析后的 HTML
├── attachments/     # 附件
├── manifests/       # 启动恢复所需 manifest
└── tmp/             # 临时文件
```

默认邮件保留时间为 **10 分钟**。后台清理任务会定期删除过期邮件记录、附件和对应落盘文件。管理后台的「清除所有邮件」会删除邮件相关数据和文件，但保留域名、管理员、API 密钥和审计日志。

## 开发

```bash
# 安装
python3 -m venv .venv
.venv/bin/pip install -c constraints-dev.txt -e ".[dev]"

# 运行全部测试
.venv/bin/pytest

# 指定测试文件
.venv/bin/pytest tests/test_admin_api.py tests/test_public_routes.py
```

项目依赖在 `pyproject.toml` 中固定到精确版本，`constraints-dev.txt` 保存一组经过验证的开发依赖解析结果。已有虚拟环境拉取新代码后，建议重新执行安装命令，确保入口脚本和依赖版本一致。

## 安全提醒

- 不要在公开环境使用默认管理员密码；如需兼容令牌访问，请配置随机的 `ADMIN_TOKEN` / `PUBLIC_API_KEY`
- SMTP 端口 `25` 在部分系统中需要管理员权限，生产部署建议通过反向代理、端口映射或专用服务账户处理
- 公开收件箱适合测试和临时场景，不建议用于接收敏感长期邮件
- `.env`、`storage/`、数据库和邮件落盘文件不应提交到 Git

安全问题请优先查看 [SECURITY.md](SECURITY.md)。

## 贡献

欢迎提交 Issue、修复和改进。开始前建议先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，里面包含开发流程、测试方式和提交 PR 的注意事项。

## 许可证

Rapid Inbox 基于 [MIT License](LICENSE) 发布。

<div align="center">

<sub>Built with ❤ for local-first email workflows</sub>

</div>
