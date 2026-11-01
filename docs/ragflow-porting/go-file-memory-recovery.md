# Go 文件、消息遗忘与密码恢复

本页只说明 `internal/` 的 Go 并行实现。生产 Python 后端的合同分别见
[运行文件权限](../references/agent-file-authorization.md)、
[Memory 权限](../references/memory-api-authorization.md) 和
[文件关联](file-link-authorization.md)。

## 运行文件与知识库关联

`GET /api/v1/agents/download?id=<upload_id>` 接受当前 JWT 或普通 API token。
ID 必须为 32 位小写十六进制；拒绝重复参数及额外参数。服务只读取当前调用者
`<user_id>-downloads` 空间中的对象与服务端 `.upload.json` 描述符，并核实描述符
身份及实际字节长度。响应固定为 `application/octet-stream`、`no-store`、
`nosniff`；缺失和越权均不泄露另一用户的对象位置。MinIO、S3、OSS 的读取传递
请求取消，整个下载最长 30 秒，释放已打开的响应体。

`POST /api/v1/files/link-to-datasets?mode=replace|add` 接受
`{"file_ids":["..."],"kb_ids":["..."]}`，默认 `replace`。
Go 在转换和清理完成后才返回布尔成功。目录递归展开后去重，并先验证原始目录、
所有叶子、有效团队关系、目标知识库以及 replace 删除权限。文件和目标知识库必须
属于同一资源租户；即使调用者同时是两个租户管理员，也拒绝跨资源租户的转换。

add 保留旧关联；replace 保留仍选中的文档，只退休不再选中的文档。
按文件排序取得 PostgreSQL 会话 advisory lock，覆盖新增提交及旧文档退休；
SQL 行锁按 Document、Knowledgebase、File 顺序获取。复制对象后读回字节，
文档、映射与计数在同一 SQL 事务提交；复制或提交失败补偿本次对象并验证不存在。
退休使用现有严格删除服务，涵盖任务取消、队列围栏、索引和精确对象地址引用检查。
清空关联保留可读取的源文件。

跨 SQL、对象、索引和 Redis 不提供整批原子事务。新增提交之后退休失败会返回失败，
已提交的新关联保留；重试以已有关联去重。存储 Put 仍使用同步旧接口，必须等待在途
写入返回才补偿；下载的取消保证不代表 Put 有可取消或有界的底层传输。

## 消息遗忘

`DELETE /api/v1/messages/<memory_id>:<message_id>` 接受非负十进制 message ID，
包括合法的 `0`，拒绝符号、空白和溢出。沿当前所有者或有效团队权限检查，持有
Memory、用户、资源租户和成员关系的 SQL share lock 直至存储操作完成。
成功响应为布尔 `true`，不存在的消息可幂等完成；不存在的表、索引或不可用存储会失败。

专用 `MemoryMessageStore` 不经过带 `kb_id` 约束的 chunk 更新路径：

| 后端 | 地址与写入 |
| --- | --- |
| Elasticsearch | `memory_<tenant>` 索引，物理 `_id=<memory>_<message>`；验证 source 的 `memory_id` 和数值 `message_id`，以 seq/primary term 乐观更新并独立读回。 |
| Infinity | `memory_<tenant>_<memory>` 表，`id=<memory>_<message>`；验证三个身份字段，以空 `forget_at`/`forget_at_flt` 对进行 CAS，并读回确认。 |
| Milvus | 缺少兼容的 Memory 消息写入基盘，明确失败，不报告假成功。 |

两种写入均保留首个遗忘时间。Infinity 同时写入 UTC 秒精度日期字符串与该瞬间的
Unix 毫秒值；读回遵守当前 `float` 列的 float32 表示，不将毫秒误写为秒。
缺失、歧义、字段类型错误、零命中而仍未遗忘或读回失败均不能作为写入成功。

Infinity 固定 Go SDK 的 Thrift RPC 使用 `context.Background()`，没有可取消传输
接口。每次操作使用独立连接，同步等待已接受的 RPC、读回和断开后才释放 SQL 授权；
取消在写入前检查，在途写入后仍完成确认。此路径不承诺在途 RPC 的最长响应时间，
也不创建取消后提前返回的后台 goroutine。

## 密码恢复

四个公开 POST 路由为 `/api/v1/auth/password/forgot/captcha?email=...`、
`/api/v1/auth/password/forgot/otp`、`/api/v1/auth/password/forgot/otp/verify`、
`/api/v1/auth/password/reset`。验证码为原始 PNG；其余响应的 `data` 为布尔。
发送参数为 email/captcha，验证参数为 email/otp，重置参数为
email/new_password/confirm_new_password；密码沿现行 Web 合同传明文，由服务端哈希。
恢复只允许有效 local/hybrid 账户，并遵守 `disable_password_login`。

OTP 验证成功时签发独立的 `multirag_recovery` cookie：256 位随机值、五分钟、
HttpOnly、SameSite=Strict、host-only、Path=`/api/v1/auth/password`。
除明文 HTTP loopback 开发环境外使用 Secure；接受的 HTTPS Origin 也强制 Secure。
Redis 的 `otp:go:verified:<email>` 只保存该值的 SHA-256 摘要。
重置必须原子匹配并消费同一客户端凭证，成功后删除 cookie；没有 cookie、错误或过期
cookie、历史 Python 邮箱标记均不能授权 Go。现有登录 token 和最后登录时间不改变。

默认只接受请求直接 scheme/Host 对应的同源 Origin；反向代理的 forwarded header
不会自动授予信任。确需代理 HTTPS 或同站跨端口访问时配置精确来源：

```yaml
authentication:
  password_recovery_origins:
    - https://app.example.test
    - http://localhost:5173
```

允许的 Origin 得到显式的 credentialed CORS 响应。浏览器调用须携带
`credentials: 'include'`；cookie 的 Strict 策略只支持同站跨源，跨站访问拒绝。
Origin 为 null、未列明来源或 cross-site fetch metadata 的请求在副作用前拒绝。
无 Origin 的 CLI 需合法恢复 cookie，浏览器 same-site metadata 不能绕过 Origin 检查。

OTP 有五分钟期限、五次尝试上限、30 分钟锁和 60 秒重发间隔。
Redis Lua 原子处理验证码消费、尝试、锁、发送 nonce 和恢复凭证。
SMTP 失败仅在 nonce/code 仍相同的情况下恢复旧值及剩余 TTL；已接受 DATA 的邮件
不会因为 QUIT 或调用方随后取消而撤销 OTP。SQL 回调失败后，以最长五秒的独立
READ COMMITTED 事务执行 `FOR UPDATE`，等待原事务结束，并在持锁期间补偿绑定凭证
及剩余 TTL，同一 cookie 可重试。回调成功而 COMMIT 报错属于结果不确定；只有锁定
读回本次唯一密码哈希才能识别已提交。其他哈希、读回失败或超时均保留防重试占位，
不恢复已消费凭证，客户端需稍后重新完成 OTP 验证；普通 MVCC 旧值不能证明未提交。

`smtp` 配置使用 `mail_server`、`mail_port`、`mail_use_ssl`、`mail_use_tls`、
`mail_username`、`mail_password`、`mail_from_name`、`mail_from_address` 和
`timeout_seconds`；期限默认 10 秒，可设 1–120 秒。带用户名的 SMTP 认证必须加密。
错误和日志不回显凭据、验证码或 OTP。
本地 SMTP 验证使用无认证的自有 loopback 服务；TLS/STARTTLS 与认证未做实际运行验收，
相关限制仅作源码核对。

上述绑定只修改 Go；Python 后续单独的[恢复流程补修](python-recovery-followup.md)
另有验证与交付证据，不能用本页的 Go 结果代替。上述同源及跨源 CORS 兼容验证也不代表
生产恢复流程已完成验收。

## 本地验证入口

`TestFileLinkScratchLifecycle`、`TestFileLinkReplaceSerializesRetirement`、
`TestDocumentDeleteRegressionScratch`、`TestMemoryForgetScratch`、
`TestForgotPasswordScratch`、`TestForgotSMTPAcceptedBeforeCancellation` 和
`TestAfterDCFRouterScratch` 读取 `MULTIRAG_AFTERDCF_G_CONFIG` 私有 JSON。
其中 Database 为 loopback PostgreSQL `postgres` 管理库；Redis 为 loopback DB 1；
MinIO 不设 Bucket/PrefixPath；Router 用真实 Elasticsearch 配置。
测试先登记再创建独立数据库、桶、索引和 Redis key，使用独立连接检查副作用与清理。
SMTP 为自有 loopback 协议边界，不向外部邮箱发信。

`TestMemoryMessageInfinityScratch` 读取 `MULTIRAG_AFTERDCF_G_INFINITY_CONFIG`，
字段为 `server.InfinityConfig`，连接独立 loopback Thrift 服务的 `default_db`，
只创建并清理随机登记的 Memory 表。运行时使用 `REQUIRE_SERVICES=1`，缺环境失败。
端点、SDK、并发与 race 证据来自注册 runner 的源码、配置、原始退出码和清理日志；
这些定向证据不替代最终组合 CI，也不证明生产或真实 SMTP/云存储验收。
