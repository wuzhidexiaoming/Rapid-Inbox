# Rapid Inbox

Rapid Inbox 是一个本地优先的临时收件服务。它通过 SMTP 接收邮件，将原始邮件、解析结果、附件和审计信息落到本地磁盘与 SQLite，并提供公开收件箱、管理后台和 HTTP API，适合验证码收件、测试环境邮件捕获、内部工具联调和轻量自托管场景。

项目目前处于早期版本，核心目标是把“收得到、看得清、管得住、容易恢复”做好，而不是依赖外部邮件服务或云端数据库。

## 特性

- 内置 SMTP 监听器，支持和 HTTP 服务同进程启动，也支持单独启动 SMTP 进程。
- 公开收件箱页面：按邮箱地址查看邮件列表、详情、原文、HTML 预览和附件。
- 实时收件体验：公开收件箱通过 WebSocket 更新，管理后台通过 SSE 查看 SMTP 接收事件。
- 管理后台：域名管理、DNS 检查、邮箱管理、邮件重解析、API 密钥、审计日志和系统设置。
- 细粒度 API Key：支持作用域、域名授权、邮箱模式、Header/Query 使用方式、IP 白名单、限速、过期和禁用/吊销。
- 本地持久化：SQLite 保存索引和元数据，磁盘保存 raw/text/html/attachments/manifests。
- 启动恢复：根据持久化 manifest 修复缺失元数据，降低异常退出后的数据不一致风险。
- 自动保留策略：邮件默认保留 20 分钟，后台任务会清理过期记录和落盘文件。
- 维护操作：管理后台可清空邮件数据、删除落盘文件并压缩 SQLite 数据库。

## 技术栈

- Python 3.10+
- FastAPI
- Jinja2
- aiosmtpd
- SQLite
- Uvicorn
- WebSocket / Server-Sent Events

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -c constraints-dev.txt -e ".[dev]"
cp .env.example .env
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

首次使用 bootstrap 管理员登录后，后台会强制进入系统设置页修改初始密码；完成改密前不能访问其他后台页面。

默认启动器会使用当前工作目录作为项目运行目录。从仓库根目录启动时，数据会写入：

```text
./storage/
./storage/app.db
```

> 首次对外部署前，请务必修改 `.env` 中的管理员密码、API Token、公开 API Key 和监听地址。

## 启动方式

HTTP 与内嵌 SMTP 同进程启动：

```bash
.venv/bin/rapid-inbox-http
```

仅启动 SMTP 监听器：

```bash
.venv/bin/rapid-inbox-smtp
```

开发时也可以直接使用模块入口：

```bash
.venv/bin/uvicorn app.main:app --reload
```

注意：直接使用 `uvicorn app.main:app` 时不会启用内嵌 SMTP。需要接收 SMTP 邮件时，请使用 `rapid-inbox-http`，或另开进程运行 `rapid-inbox-smtp`。

## 配置

启动器会优先读取真实环境变量，其次读取当前工作目录下的 `.env`，最后使用代码默认值。

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
| `SMTP_IDLE_TIMEOUT_SECONDS` | `300` | SMTP 会话空闲断开时间 |
| `SMTP_MAX_CONCURRENT_CONNECTIONS` | `100` | SMTP 并发连接上限，`0` 表示不限制 |
| `SMTP_CONNECTION_RATE_LIMIT_COUNT` | `20` | 每个 IP 在短窗口内允许建立的 SMTP 连接数，`0` 表示不限制 |
| `SMTP_CONNECTION_RATE_LIMIT_WINDOW_SECONDS` | `60` | SMTP per-IP 连接限流窗口 |
| `DISK_WARNING_THRESHOLD_PERCENT` | `85` | Dashboard 磁盘使用率告警阈值 |
| `ADMIN_TOKEN` | `dev-admin-token` | 兼容管理 API 的管理令牌 |
| `PUBLIC_API_KEY` | `public-demo-key` | 兼容公开 API 的默认访问密钥 |

配置优先级：

1. 真实环境变量
2. 当前工作目录中的 `.env`
3. `app/config.py` 中的默认值

## 基本使用

1. 启动服务并登录 `/admin/login`。
2. 在管理后台添加可接收的根域名。
3. 将测试邮件投递到任意匹配邮箱地址，例如 `code@example.com`。
4. 在公开页面 `/mail/{mailbox_address}` 或管理后台查看邮件。
5. 使用 API Key 为测试脚本、内部工具或自动化流程读取邮件。

公开页面入口：

```text
GET /
GET /mail/{mailbox_address}
GET /mail/{mailbox_address}/{delivery_id}
GET /mail/{mailbox_address}/{delivery_id}/raw
GET /mail/{mailbox_address}/{delivery_id}/attachments/{attachment_id}
```

公开 API 示例：

```bash
curl \
  -H "X-API-Key: public-demo-key" \
  "http://127.0.0.1:8000/api/v1/public/mailboxes/code@example.com/messages"
```

公开 API 列表支持 `limit`、兼容旧版的 `offset`，并返回 `next_cursor`。新集成建议使用 `next_cursor` 继续翻页：

```bash
curl \
  -H "X-API-Key: public-demo-key" \
  "http://127.0.0.1:8000/api/v1/public/mailboxes/code@example.com/messages?limit=20&cursor=<next_cursor>"
```

## 数据与保留策略

Rapid Inbox 使用 SQLite 保存结构化数据，并将邮件内容拆分保存在本地目录中：

```text
storage/
  app.db
  raw/
  text/
  html/
  attachments/
  manifests/
  tmp/
```

默认邮件保留时间为 20 分钟。后台清理任务会定期删除过期邮件记录、附件和对应落盘文件。管理后台的“清除所有邮件”会删除邮件相关数据和文件，但会保留域名、管理员、API 密钥和审计日志。

## 开发

安装开发依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install -c constraints-dev.txt -e ".[dev]"
```

运行测试：

```bash
.venv/bin/pytest
```

仅运行一组测试：

```bash
.venv/bin/pytest tests/test_admin_api.py tests/test_public_routes.py
```

项目依赖在 `pyproject.toml` 中固定到精确版本，`constraints-dev.txt` 保存一组经过验证的开发依赖解析结果。已有虚拟环境拉取新代码后，建议重新执行安装命令，确保入口脚本和依赖版本一致。

## 安全提醒

- 不要在公开环境使用默认管理员密码、默认 `ADMIN_TOKEN` 或默认 `PUBLIC_API_KEY`。
- SMTP 端口 `25` 在部分系统中需要管理员权限，生产部署时建议通过反向代理、端口映射或专用服务账户处理。
- 公开收件箱适合测试和临时场景，不建议用于接收敏感长期邮件。
- `.env`、`storage/`、数据库和邮件落盘文件不应提交到 Git。

安全问题请优先查看 [SECURITY.md](SECURITY.md)。

## 贡献

欢迎提交 Issue、修复和改进。开始前建议先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，里面包含开发流程、测试方式和提交 PR 的注意事项。

## 许可证

Rapid Inbox 基于 [MIT License](LICENSE) 发布。
