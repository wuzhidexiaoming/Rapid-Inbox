# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 和 [语义化版本](https://semver.org/lang/zh-CN/) 的思路记录重要变化。当前处于 `0.x` 阶段，接口和数据结构可能继续调整。

## [Unreleased]

### 新增

- 打分制验证码识别：支持中英日韩西多语言上下文提示、字母数字/分隔符组合（如 `123-456`、`A3F9B2`）和 HTML 富文本场景；对订单号、年份、电话号码、URL 中的数字更稳健地排除；两候选平分或出现「X or Y」歧义时主动弃权。
- 33 个验证码提取单元测试，覆盖真实邮件中常见的各种形态。
- 邮件自动保留与过期清理能力。
- C++ `rapid-inbox-ingestd` 高吞吐 SMTP 收件入口，保留 Python HTTP 后台与公开页面，并写入现有 SQLite/storage 数据契约。
- `quickstart.sh` 一键快速开始脚本，自动准备 Python 环境、构建 C++ ingestd 并启动本地服务。
- GitHub Actions ingestd 发版流程：push/PR 自动构建测试，`v*` tag 自动发布 Linux x86_64 预编译二进制。
- SMTP 验证码压测脚本，可批量投递验证码邮件并采样 C++ ingestd / Python HTTP 的 CPU 与内存。

### 变更

- SMTP 监听器按 `MAX_MESSAGE_SIZE_BYTES` 传入 `data_size_limit`，避免被 aiosmtpd 默认 32 MB 限制截断。
- 管理员登录失败时也写入审计日志，方便事后追踪爆破尝试。
- 首页 Hero 标题文案改为「一个地址，就能收下公开邮件」，取消中文假斜体并放宽行距。
- 完善 API 密钥编辑、授权范围和状态管理。
- 增强清空邮件数据后的文件清理和 SQLite 压缩流程。
- 整理 README、贡献指南、安全策略和 GitHub 协作模板。
- `quickstart.sh` 默认优先下载 GitHub Release 预编译 ingestd，下载失败或指定 `--build-local` 时回退到本地编译。

### 修复

- 清理 SMTP per-IP 限流窗口中的过期条目，防止长期运行后内存缓慢增长。
- 修正 `_apply_parsed_message` 中 INSERT attachment 的缩进风格。
- 更正 README：邮件默认保留时间从 20 分钟修正为 10 分钟，与代码和测试一致。
- 修复代理或预览环境下管理员登录可能被 Origin 校验误拦为 `invalid origin` 的问题。

## [0.1.0]

### 新增

- SMTP 收件、公开收件箱、管理后台和 HTTP API 的基础能力。
- 本地 SQLite 与磁盘文件持久化。
- 域名、邮箱、消息、附件、API Key、审计和系统设置管理。
- 启动恢复、邮件解析、HTML 预览和实时收件更新。
