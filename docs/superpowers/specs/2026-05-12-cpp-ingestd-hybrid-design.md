# C/C++ 混合高吞吐收件架构设计

## 背景

Rapid Inbox 当前使用 Python、FastAPI、aiosmtpd、SQLite 和本地文件系统实现临时邮箱服务。现有实现的优势是开发效率高、后台和公开页面完整、数据模型清晰；主要吞吐压力集中在 SMTP 接收热路径、每封邮件写文件和 SQLite、MIME 解析、附件展开等环节。

本设计采用分阶段混合架构：保留现有 Python/FastAPI 后台、公开页面、权限、管理 API 和数据模型，新增 C/C++ 高吞吐 SMTP ingest 进程接管生产收信入口。目标是在低资源消耗下稳定处理每秒 1000+ 封邮件的 SMTP 接收成功路径。

## 目标

- SMTP `DATA` 成功后尽快返回 `250 queued as <message_id>`。
- `250` 前只要求邮件进入 C/C++ ingest 进程内存队列。
- 正常停止或重启时必须 drain 已返回 `250` 的内存队列，写入 storage 和 SQLite 后再退出。
- 进程崩溃、机器重启或断电时，允许丢失尚未落盘的内存队列邮件。
- 已接收邮件通常在 500ms 到 1s 内出现在现有后台、公开收件箱和公开 API 中。
- 第一阶段不破坏现有 Python 页面/API/权限/测试体系。

## 非目标

- 第一阶段不全量重写 Python/FastAPI 后台。
- 第一阶段不改变公开 API 路由、后台模板、管理员权限模型。
- 不承诺崩溃或断电零丢失。
- 不在 SMTP 热路径中做垃圾邮件过滤、杀毒、SPF/DKIM/DMARC 判断。

## 总体架构

新增独立进程 `rapid-inbox-ingestd`，生产环境 SMTP 流量进入该进程。现有 `rapid-inbox-http` 继续运行 Python HTTP 服务，但生产部署不启用 Python 内嵌 SMTP。

`rapid-inbox-ingestd` 内部组件：

- SMTP acceptor 和 connection workers：处理连接、命令、RCPT 校验和 DATA 收取。
- Domain rule cache：从 SQLite `domains` 表加载可收域名规则，定时刷新或通过信号刷新。
- In-memory mail queue：保存已完成 DATA、可返回 `250` 的邮件 job。
- Batch storage writer：批量写 manifest、raw 文件和 SQLite 占位记录。
- Parser workers：后续阶段迁移 MIME 解析、正文/附件落盘和 message 元数据更新。
- Shutdown coordinator：控制停止接新连接、等待活跃 DATA、drain 队列和退出。

## SMTP 语义

`rapid-inbox-ingestd` 的 `250 queued as <message_id>` 表示：

- 收件人域名已通过当前 domain cache 校验。
- DATA 内容已完整接收进进程内存。
- mail job 已成功放入内存队列。

该语义不表示邮件已经写入 SQLite 或磁盘。正常停止时队列必须 drain；异常崩溃时允许丢失尚未写出的 job。

## 数据流

1. 客户端连接 SMTP。
2. connection worker 创建轻量 session 状态。
3. `RCPT TO` 阶段使用 domain cache 校验域名、exact/subdomain、plus addressing、local-part 大小写规则和收件人数限制。
4. `DATA` 阶段收完整邮件内容，检查全局和域名级消息大小上限。
5. ingestd 生成 `message_id`、每个收件人的 `delivery_id`、`received_at`、raw sha256、raw size 和 canonical mailbox 地址。
6. mail job 写入 bounded in-memory queue。
7. SMTP 返回 `250 queued as <message_id>`。
8. batch writer 按消息数量或时间窗口聚合 job。
9. batch writer 先写 `storage/manifests/YYYY/MM/DD/<message_id>.json` 和 `storage/raw/YYYY/MM/DD/<message_id>.eml`。
10. batch writer 在一个 SQLite 事务中批量写 `smtp_sessions`、`messages`、`mailboxes`、`message_deliveries`、`mail_metric_buckets`，可选写 `smtp_events`。
11. 事务提交成功后 job 从 drain backlog 中移除。
12. parser workers 或 Python 解析队列异步处理 `parse_status='pending'` 的 message。

## SQLite 和文件兼容契约

C/C++ 写入必须遵守现有 Python 代码依赖的数据契约：

- raw 文件路径：`raw/YYYY/MM/DD/<message_id>.eml`。
- manifest 路径：`manifests/YYYY/MM/DD/<message_id>.json`。
- 文本正文路径：`text/YYYY/MM/DD/<message_id>.txt`。
- HTML 正文路径：`html/YYYY/MM/DD/<message_id>.html`。
- 附件路径：`attachments/<message_id>/<attachment_id>-<safe_name>`。
- `messages.id` 使用 `msg_<uuid>`。
- `message_deliveries.id` 使用 `dlv_<uuid>`。
- 首次写入 message 时可用 `parse_status='pending'`，`subject=NULL`，`from_addr=envelope_from`。
- mailbox upsert、summary 刷新和 delivery 关联必须与现有公开页面/API 读取逻辑兼容。

第一阶段尽量不改 `sqlite_schema.sql`。如果后续确实需要 ingest 专用统计或 checkpoint 表，必须通过轻量迁移新增表，不能破坏现有表语义。

## Domain 匹配规则

C/C++ domain cache 必须复刻当前 Python matcher：

- 域名使用 lowercase 和 IDNA ASCII。
- 多条规则命中时选择最长 root domain。
- 精确域名要求 `accept_exact=true`。
- 子域要求 `accept_subdomains=true`。
- `plus_addressing_mode='strip'` 时 canonical local-part 去掉 `+` 后缀。
- `local_part_case_sensitive=false` 时 canonical local-part 转小写。
- 只加载 `is_active=1` 的 domain 规则。

需要建立 C/C++ 与 Python matcher 的一致性测试，覆盖 IDNA、子域、最长后缀、plus addressing 和大小写。

## 批量写入策略

SMTP connection workers 不直接写 SQLite。所有写入由 batch writer 串行或少量分片执行。

建议初始配置：

- `INGEST_QUEUE_MAX_MESSAGES`：内存队列上限，超过后新 DATA 返回临时失败。
- `INGEST_BATCH_MAX_MESSAGES`：100 到 500。
- `INGEST_FLUSH_INTERVAL_MS`：250ms，最大不超过 1000ms。
- `INGEST_SQLITE_BUSY_TIMEOUT_MS`：5000ms。
- `INGEST_STORAGE_FSYNC`：默认关闭，匹配当前性能优先语义。

批量事务需要控制大小，避免长事务阻塞 Python HTTP 读请求。若写入延迟升高，优先缩小 batch 或降低 flush interval，而不是让 SMTP 线程参与写库。

## 正常停止和重启

收到 SIGTERM 或管理命令后：

1. 关闭监听 socket，停止接受新 SMTP 连接。
2. 对未进入 DATA 或未完成 DATA 的连接返回临时失败或断开。
3. 对已经返回 `250` 的 mail job，等待 batch writer 写入 storage 和 SQLite。
4. flush parser worker 的已提交结果；未完成解析的 message 可以保持 `pending`，由后续启动恢复。
5. 队列清空后退出。

如果 drain 超过配置的最大等待时间，默认继续等待并记录告警；生产环境可显式配置强制退出策略，但强制退出会丢失内存队列中未写出的邮件。

## 解析迁移策略

第一阶段接管 SMTP ingest 和批量占位写入，解析可以继续由 Python 现有 `ParseQueue` 处理 `pending` 邮件。这样先验证 1000+/秒 `250` 目标，不让 MIME 边角问题阻塞收信热路径迁移。

第二阶段在 C/C++ 中引入 parser workers，使用成熟 MIME 库解析：

- message headers。
- text/plain 和 text/html。
- inline 和普通附件。
- content-id 引用所需元数据。
- text preview。

验证码提取可作为后续独立迁移项。第一版 C/C++ parser 不必一次性复刻全部验证码评分语义。

## 错误处理

- domain 不允许：SMTP 返回 `550 domain not allowed`。
- 收件人数超限：SMTP 返回 `552 too many recipients`。
- 消息大小超限：SMTP 返回 `552 message too large`。
- 内存队列已满：SMTP 返回 `451 temporary queue full`。
- DATA 尚未入队时出现内部错误：SMTP 返回 `451 temporary local error`。
- 已返回 `250` 后 writer 失败：持续重试并阻塞正常 drain；错误需要进入 ingestd 日志和健康状态。

writer 写 storage 成功但 SQLite 失败时，必须保留 manifest/raw，让现有或新增恢复流程可以补录。第一阶段可以复用当前 manifest 设计，必要时补充 C/C++ 写入的 manifest 字段。

## 运维和配置

新增进程建议提供：

- `rapid-inbox-ingestd --config .env` 或读取现有环境变量。
- `SMTP_HOST`、`SMTP_PORT`、`STORAGE_ROOT`、`DATABASE_PATH` 与当前配置保持兼容。
- ingest 专用队列、batch、flush、worker 数量配置使用 `INGEST_*` 前缀。
- `/health` 本地管理端口或 Unix socket，暴露 queue depth、writer lag、batch commit latency、accepted rate、rejected rate、RSS 和 CPU 统计。

部署形态：

- 开发环境可以继续使用 `rapid-inbox-http` 内嵌 SMTP。
- 生产环境运行 `rapid-inbox-http` 加 `rapid-inbox-ingestd`。
- README 需要明确两种启动模式和 `250` 持久化语义差异。

## 测试计划

- C/C++ domain matcher 单元测试：与 Python matcher 用例保持一致。
- C/C++ storage path 单元测试：路径格式、ID 生成、相对路径安全。
- SQLite 兼容测试：ingestd 写入后，现有 Python public/admin 页面和 API 能读取邮件。
- 正常停机测试：返回过 `250` 的邮件全部写入后进程退出。
- 崩溃语义测试：kill -9 后允许丢失未落盘内存队列，不破坏已有 SQLite/storage。
- 压测：目标 `1000+ DATA accepted/s`，采集 p50/p95 `250` 延迟、writer backlog、SQLite commit 延迟、CPU、RSS、磁盘写入量。
- 回归：继续运行现有 Python 测试套件。

## 分阶段实施

### 阶段 1：C/C++ ingestd MVP

- 建立 C/C++ 工程骨架。
- 实现 SMTP acceptor、domain cache、内存队列。
- 实现 batch writer 写 raw、manifest、SQLite pending 记录。
- 实现正常停止 drain。
- 保留 Python 解析和 HTTP。
- 完成黑盒 SMTP 测试和 1000+/秒接收压测。

### 阶段 2：解析迁移

- 引入 C/C++ MIME parser workers。
- 写 text/html/attachments 并更新 `messages`、`attachments`。
- 保留 Python reparse 作为兜底路径或管理动作。
- 对真实邮件格式进行兼容测试。

### 阶段 3：性能收敛和运维完善

- 加入 ingestd 健康检查和指标。
- 调整 batch、flush、queue 参数。
- 优化 SQLite 事务大小和索引写入成本。
- 更新 README、部署文档和安全说明。

## 风险

- C/C++ 直接写现有 SQLite 和 storage，必须严格维护与 Python 的数据契约。
- SQLite 单写者模型仍是全局写入上限，需要通过批量事务和短事务控制延迟。
- `250` 前不落盘会带来明确的数据丢失窗口。
- MIME 解析边角多，第二阶段迁移需要真实邮件样本和回归测试支撑。
- 两个进程共享 SQLite 时，需要谨慎处理 busy timeout、事务大小和读写延迟。
