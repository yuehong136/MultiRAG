# Go API 行为

生产后端使用 Python/FastAPI；本目录是 Go 并行实现。构建和 native 准备见
[开发说明](../docs/development.md#go-并行实现的本地验证)。三个入口分别为
`server_main.go`、`admin_server.go` 和 `multirag_cli.go`，应分别构建。

以下 API 使用现有认证中间件和用户状态检查：

| 接口 | 行为 |
| --- | --- |
| `POST /api/v1/connectors/` | 为当前用户保存配置；不联系源端，不调度同步。默认 refresh/prune 为 5，timeout 为 1740 秒；显式频率 0 保留。响应不含配置或凭据。 |
| `GET /api/v1/connectors/` | 列出自有和具有有效 owner/admin/normal membership 的连接器，返回公开字段。 |
| `GET /api/v1/connectors/{id}/logs` | page 默认 1、page_size 默认 15，均至少为 1，每页最多 100。重复、空、非整数参数失败；仅返回可见数据集的日志，结果按更新时间和 ID 稳定倒序。错误详情经过脱敏，保留状态和错误数量。 |
| `DELETE /api/v1/connectors/{id}` | 在同一 SQL 事务中取消待执行/运行中日志、移除绑定和连接器；保留已导入文档。 |
| `POST /api/v1/connectors/{id}/rebuild` | 请求体为 `{"kb_id":"..."}`。先检查资源访问、同租户数据集和已保存绑定，再删除该连接器在该数据集中的索引和 SQL 文档，重置计数并写入 sync 调度日志；开启 `sync_deleted_files` 时再写 prune 日志。索引错误保留 SQL 记录以便重试。 |
| `PATCH /api/v1/users/me` | 部分更新 nickname/avatar/language/color_schema/timezone。密码更新须同时提供 password 和 new_password；沿用登录的 RSA 加密、内层 Base64 协议，并验证旧密码。未指定字段保留，不允许修改 email。 |
| `PATCH /api/v1/users/me/models` | 仅管理当前用户自有租户。支持 llm_id/embd_id/asr_id/img2txt_id/rerank_id/tts_id 部分更新，空字符串显式清空；模型名及可选 `@factory` 必须唯一匹配该租户的有效、对应能力模型，同时写入解析出的 tenant model ID。 |
| `POST /api/v1/mcp/servers` | 保存自有 MCP 服务器，支持 sse/streamable-http；验证 URL、对象配置和 headers，重置 tools。租户行锁保证同租户并发重名只创建一次。保存时不外连，响应不回显 headers 和私有 variables。 |

上述写接口拒绝未知、重复、null 或尾随 JSON 字段；验证错误不回显请求值。
连接器重建跨 SQL 和索引，不是跨存储事务；索引删除成功后的 SQL 失败保留 SQL 可重试状态。
原始对象存储回收沿用已有生命周期，不由这些 Go API 执行。调度日志落库与真实 worker 执行是不同的验证层。

`GET /api/v1/system/healthz` 为公开依赖就绪检查，检查 DB、Redis、文档引擎和对象存储，
任一失败返回 HTTP 500 与 `nok`，各依赖包含耗时、脱敏错误和失败类型（uninitialized/unsupported/timeout/probe_failed）；使用有界、只读探测，不写测试键或对象。
当前 Infinity SDK 的 Ping 仅返回连接标志，缺少有界活动探针；该引擎的 healthz 明确返回 nok，避免误报。
`GET /health` 保持进程存活检查。

定向回归位于 `internal/router/after7fb_api_live_test.go`、
`internal/service/system_healthz_test.go`、`internal/handler/object_json_test.go` 和
`internal/storage/readiness_test.go`。SQL/HTTP 测试要求显式提供隔离数据库、私有配置目录和监听地址；
设置 `REQUIRE_SERVICES=1` 时缺少必需环境直接失败。资源登记入口和证据范围见
[测试说明](../tests/README.md)。
