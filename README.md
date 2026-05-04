# Rapid Inbox

Rapid Inbox 是一个本地优先的收件服务。它通过 SMTP 接收邮件，将原始邮件和元数据存储到磁盘，并提供公开端与管理端 HTTP 页面用于浏览和操作。

## 本地开发

1. `python3 -m venv .venv`
2. `.venv/bin/pip install -c constraints-dev.txt -e .[dev]`
3. `.venv/bin/rapid-inbox-http`
4. 打开 `http://127.0.0.1:8000/admin/login`

默认的 HTTP 启动器会在同一个进程中启动 FastAPI 应用和内嵌 SMTP 监听器，并使用当前工作目录作为存储根目录。从仓库根目录运行时，会创建 `./storage/` 和 `./storage/app.db`。
如果你需要为自定义部署单独运行 SMTP 监听器，也可以在另一个终端中执行 `.venv/bin/rapid-inbox-smtp`。

## 默认值

启动默认配置定义在 `app/config.py` 中。当前启动器会优先从当前工作目录加载 `.env`，再回退到代码默认值：

- 初始管理员用户名：`admin`
- 初始管理员密码：`change-me-now`
- Session Cookie 名称：`rapid_inbox_session`
- HTTP 监听地址和端口：`127.0.0.1:8000`
- SMTP 监听地址和端口：`127.0.0.1:25`
- 单封邮件最大体积：`52428800`
- 单封邮件最大收件人数：`20`
- 邮件保留时间：`20` 分钟，过期后后台任务会自动删除邮件记录和落盘文件

默认启动流程会自动创建初始管理员账号，用户名为 `admin`、密码为 `change-me-now`，因此在全新本地环境中可以直接登录。

配置优先级如下：

1. 真实环境变量
2. 项目根目录或当前工作目录中的 `.env`
3. `app/config.py` 中的代码默认值

这意味着你可以把 `.env.example` 复制为 `.env` 并按需修改，默认的 `rapid-inbox-http` / `rapid-inbox-smtp` 启动器会自动读取它。

## 依赖锁定

`pyproject.toml` 中的直接依赖已经固定到精确版本，`constraints-dev.txt` 则包含开发环境中经过验证的完整依赖集合。

如果你希望尽量减少 pip 回溯和重复解析重试，建议优先使用：

` .venv/bin/pip install -c constraints-dev.txt -e .[dev] `

公开邮箱的实时更新功能基于 WebSocket。项目现在已将 `websockets` 声明为运行时依赖，因此如果你是在已有虚拟环境中拉取了新代码，请在重启 `rapid-inbox-http` 前重新执行上面的安装命令。

## 说明

- HTTP 启动器会使用 Uvicorn 启动 FastAPI 应用和内嵌的 `aiosmtpd` 监听器。
- SMTP 启动器会单独启动 `aiosmtpd` 监听器，并在被中断前持续运行。
- 管理后台登录页使用启动时创建的初始管理员凭据。
