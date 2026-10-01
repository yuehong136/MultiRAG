# Go 移植适配

仅用于本次涉及 Go 的移植。Go 是停滞的并行实现；不把它作为 Python 后端的扩展入口。
下列映射用于定位，具体接口以当前代码为准。

## 本地差异

- import `ragflow/` → `multirag/`，命名 `ragflow_*` → `multirag_*`，token 前缀为 `multirag-`。
- 配置目录 `conf/` → `configs/`，`internal/util/` → `internal/utility/`；复用 `GetProjectBaseDirectory()`。
- 客户端 receiver 为 `MultiRAGClient`，HTTPClient 凭据字段为 `APIKey`；核实 Response 字段，避免照抄上游 `Duration` 访问。
- handler 复用 `GetUser(c)`、`common.Code*`、`jsonError` / `jsonResponse`。
- CLI 的共享叶子解析放 `parser.go`；复用 `expectSemicolon`、`-o` / `\format` 与 `looksLikeContextEngine` 分流。新语法核对 token（含 `isKeyword` 上界）、parser、command、client 的 Execute 接线。
- 存储接口变更核对 infinity / elasticsearch / milvus 三种实现；既有桩不扩成假写入成功，新写能力缺基盘时保持不可用并说明。
- Infinity SDK 跟目标 commit 的版本，不追上游 HEAD 的无关升级。
- Go 旧名按目标提交对齐；生产 API 的退役同样按 [逐接口删除判据](python.md#行为与兼容性) 核对查明的调用和兼容承诺。命名改动参考本项目 [go-naming](../../../../.agents/skills/go-naming/SKILL.md)。

## 验证

对改动文件运行 gofmt；执行 `go build ./internal/...` 与 `go vet ./internal/...`，
`cmd/` 下各个独立 main 文件分别构建到临时输出，避免多个 main 一起编译。
CLI 语法改动保留能锁定语法行为的回归测试；live 验收按用户范围和可用隔离环境开展。

当前失败需要本机归因，不能把旧的 build / vet 告警直接当作豁免；Go 专项验证不替代同批 Python 改动的门禁。
