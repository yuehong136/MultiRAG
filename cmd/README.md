# Go 入口与 API

生产后端使用 Python；本目录保留对应的四个 Go main，分别构建：
`server_main.go`、`admin_server.go`、`ingestor.go`、`multirag_cli.go`。
工具链、native、配置和验证命令见[开发说明](../docs/development.md#go-并行实现的本地验证)。

`server_main.go` 用原 service/handler 构造器和 `router.NewRouter` 注册 API。
身份来自原鉴权中间件，业务处理还需检查活动用户、租户和资源权限。
请求与返回值以原 handler DTO 和注册方法为准；不能假定所有端点统一拒绝未知/重复 JSON、
不回显配置、提供同一种部分更新或兼容旧 numeric inference ID。
模型推理使用 UUID TenantModel，TenantLLM 管理入口沿用冻结树自己的表和字段。

`admin_server.go` 管理实际管理员请求和 heartbeat；未实现的管理操作明确失败。
`ingestor.go` 使用独立 NATS/SQL 任务路径，当前未实现的处理记录失败，不伪造成功。
CLI 通过原 HTTP client、parser 和 `filesystem/` 调用相应接口，语法见
[CLI README](../internal/cli/README.md)。

`GET /health` 是进程存活检查；`GET /api/v1/system/healthz` 按实际依赖检查就绪。
原 Skills 路径为 `/api/v1/skills`，见[Go Skills](../docs/skills/GO_CORE.md)。

实际路由、鉴权、CLI、跨租户拒绝及文件快照回归位于 `internal/router/router_test.go`，
领域 SQL/HTTP/对象存储断言位于相应 service 和 handler 测试。原 provider 接口、目录和
失败语义收敛见[专项记录](../docs/ragflow-porting/go-alignment.md)。
测试使用登记的独立资源；受控本地结果不代表供应商、远程 CI 或生产验收。
