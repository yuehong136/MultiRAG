# Go 公共日志

Go 并行实现通过 `multirag/internal/common` 使用 Zap 日志；不依赖 server、service
或 handler，因此配置加载也能直接记录日志。Python 生产后端沿用自己的日志设施。

包初始化即创建 stdout console logger，默认 info；调用 `Init` 前的 info/warn/error
不会静默丢失。`Init` 与 `SetLevel` 仅更新同一个 `zap.AtomicLevel`，保留 `Logger`、
`Sugar` 及已被 heartbeat、配置模块等捕获的对象。两者接受 debug、info、warn /
warning、error、panic、fatal；非法值报错且保持当前级别。并发读取、调级和记录安全。
导出的 `Logger` / `Sugar` 应视为只读引用，不要自行重新赋值。

```go
import (
    "multirag/internal/common"
    "go.uber.org/zap"
)

common.Info("Started", zap.String("component", "worker"))
common.Error("Request failed", err)
common.Debug("Request details", zap.String("request_id", requestID))
common.Warn("Slow request", zap.Duration("duration", duration))
// Fatal 增加调用方文件/行号，Zap flush 后以状态 1 退出。
common.Fatal("Startup failed", zap.Error(err))
```

`cmd/server_main.go` 和 `cmd/admin_server.go` 在读取配置前绑定日志，随后应用
配置中的级别；失败时保留原级别并记录错误。正常退出 defer `common.Sync()`，
显式 `os.Exit` 路径在退出前 Sync。Fatal 由 Zap core 在退出前同步。
`cmd/multirag_cli.go` 已位于独立入口目录，默认 error 级别，帮助、单命令错误及正常
返回均经统一退出路径 flush；SIGINT/SIGTERM 的直接退出也先清理终端并 flush。
CLI 库的 `RunInteractive` 信号路径同样处理。SIGKILL 不执行清理。

日志保持 console 格式，输出到 stdout；`log.format` 当前不控制该 Go 实现。
不宣称 stdlib fallback、文件轮转或持久日志文件支持。`Sync` 调用输出同步，保留
现有无返回值接口；终端/管道不支持 fsync 的错误不作为程序退出失败。

API/admin 的 log_level 端点使用同一动态级别。验证入口：

```sh
go test -race ./internal/common ./internal/server ./internal/handler ./internal/admin \
  -run 'TestLog|TestConfigLogsBeforeLoggerInit|TestSystemLogLevelHTTP|TestAdminLogLevelHTTP'
go build ./internal/...
go vet ./internal/...
go build -o /tmp/multirag-server cmd/server_main.go
go build -o /tmp/multirag-admin cmd/admin_server.go
go build -o /tmp/multirag-cli cmd/multirag_cli.go
```
