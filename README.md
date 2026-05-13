<div align="center">

# Rapid Inbox

**本地优先的临时邮箱服务**

高吞吐 C++ SMTP 收件入口、公开收件箱、管理后台和 HTTP API<br/>
邮件、附件、元数据和审计全部落本地磁盘与 SQLite

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![C++](https://img.shields.io/badge/C%2B%2B-20-00599C?logo=cplusplus&logoColor=white)](https://isocpp.org)
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
| **邮件接收** | C++ `rapid-inbox-ingestd` 高吞吐 SMTP 收件入口；Python 内嵌 SMTP 保留为开发/兼容模式 |
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

`C++20` · `Python 3.10+` · `FastAPI` · `aiosmtpd` · `Jinja2` · `SQLite` · `Uvicorn` · `WebSocket` · `SSE`

## 快速开始

```bash
bash quickstart.sh
```

脚本会自动创建 `.venv`、安装依赖、复制 `.env.example`，默认从 GitHub Releases 下载预编译的 C++ `rapid-inbox-ingestd`，并启动 Python HTTP + C++ SMTP 收件入口。默认绑定：

```text
HTTP: 0.0.0.0:8000
SMTP: 0.0.0.0:25
```

默认 quickstart 会在 `0.0.0.0:25` 启动 C++ SMTP ingestd。邮件元数据、text/html 正文、附件和验证码会由 ingestd 直接写入现有 SQLite 数据库和 `storage/` 目录；Python 服务只负责 HTTP、管理后台和公开 API。

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

如果希望强制本地编译 C++ ingestd，而不是下载 GitHub Release 二进制：

```bash
bash quickstart.sh --build-local
```

如果要下载指定版本或指定二进制地址：

```bash
bash quickstart.sh --ingestd-version v0.1.0
bash quickstart.sh --binary-url https://example.com/rapid-inbox-ingestd-linux-x86_64.tar.gz
```

> 当前预编译二进制目标为 Linux x86_64。非 Linux x86_64、下载失败或指定 `--build-local` 时，quickstart 会回退到本地编译。

如果本机需要本地编译，可先安装：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip cmake g++ libsqlite3-dev libssl-dev libunistring-dev libicu-dev
```

只想使用 Python 内嵌 SMTP 兼容模式时：

```bash
bash quickstart.sh --python-smtp
```

## 启动方式

<details>
<summary><b>C++ SMTP ingestd + Python HTTP</b>（高吞吐生产模式，推荐）</summary>

```bash
# 1. 构建 C++ SMTP 收件入口
cmake -S cpp/ingestd -B cpp/ingestd/build
cmake --build cpp/ingestd/build

# 2. 启动 Python HTTP，不启用内嵌 SMTP
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3. 启动 C++ SMTP 收件入口
SMTP_HOST=0.0.0.0 SMTP_PORT=25 cpp/ingestd/build/rapid-inbox-ingestd --base-dir .
```

`250 queued` 表示邮件已进入 ingestd 进程内存队列。正常停止会 drain 已返回 `250` 的邮件并写入 storage/SQLite；异常崩溃、断电或 `kill -9` 可能丢失尚未落盘的内存队列邮件。

</details>

<details>
<summary><b>HTTP + Python 内嵌 SMTP 同进程</b>（开发/兼容模式）</summary>

```bash
.venv/bin/rapid-inbox-http
```

</details>

<details>
<summary><b>仅启动 Python SMTP 监听器</b>（兼容模式）</summary>

```bash
.venv/bin/rapid-inbox-smtp
```

</details>

<details>
<summary><b>开发模式（模块入口）</b></summary>

```bash
.venv/bin/uvicorn app.main:app --reload
```

直接使用 `uvicorn app.main:app` 时**不会**启用内嵌 SMTP。需要接收 SMTP 邮件时，生产推荐另开进程运行 `rapid-inbox-ingestd`，开发可使用 `rapid-inbox-http` 或 `rapid-inbox-smtp`。

</details>

## 发布二进制

仓库包含 GitHub Actions 工作流 `.github/workflows/release-ingestd.yml`：

- 普通 push / pull request：运行 Python 测试并构建、测试 C++ ingestd。
- 推送 `v*` tag：构建 Linux x86_64 release 包，并把以下文件发布到 GitHub Release：
  - `rapid-inbox-ingestd-linux-x86_64.tar.gz`
  - `rapid-inbox-ingestd-linux-x86_64.tar.gz.sha256`

发版示例：

```bash
git tag v0.1.0
git push origin v0.1.0
```

Release 发布完成后，`bash quickstart.sh` 会默认从 latest release 下载预编译 ingestd。需要本地编译时使用 `--build-local`。

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
| `HOST` | `0.0.0.0` | HTTP 监听地址 |
| `PORT` | `8000` | HTTP 监听端口 |
| `SMTP_HOST` | `0.0.0.0` | SMTP 监听地址 |
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

# C++ ingestd 测试
cmake -S cpp/ingestd -B cpp/ingestd/build
cmake --build cpp/ingestd/build
ctest --test-dir cpp/ingestd/build --output-on-failure

# 指定测试文件
.venv/bin/pytest tests/test_admin_api.py tests/test_public_routes.py
```

### SMTP 压测

可使用内置脚本批量投递验证码邮件并采样 C++ ingestd / Python HTTP 的 CPU 与内存：

```bash
./tools/smtp_stress_test.py --count 5000 --concurrency 100 --json-output .rapid-inbox-run/stress.json
```

更大压力示例：

```bash
./tools/smtp_stress_test.py --count 20000 --concurrency 200 --json-output .rapid-inbox-run/stress-20000.json
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
