# Go 移植适配

用于目标包含 Go 或用户点名 Go 的移植。生产后端仍是 Python；Go 对齐不授权生产切换。
按 [共用准则](../SKILL.md#保持上游结构与做法) 以目标树为准，不把本地旧布局作为永久约束。

## 映射与调用方

- import `ragflow/` → `multirag/`，命名 `ragflow_*` → `multirag_*`，token 前缀为 `multirag-`。
- 配置和路径先查目标及 Python 的实际读取边界。必要仓路径映射单列记录；不为共用配置保留旧 Go 身份、默认值或数据合同，不改动生产 Python 数据。
- 客户端、handler、CLI、driver 的文件和函数保持目标对应。签名变化同步迁移源码调用方，删除旧别名、无调用包装、facade 和 fallback；不按本地已有拆分再新建文件。
- 存储接口按目标引擎及真实调用链迁移；本地额外后端不是保留旧接口的理由，也不是目标漏项。占位不能变成无副作用成功。
- Infinity SDK 跟目标 commit 的版本，不追上游 HEAD 的无关升级。
- Go 尚未部署，不为其旧接口和旧结构保留兼容。涉及 Python 生产共享合同先查证具体影响；普通 Go 重构不因此另索授权。命名改动参考本项目 [go-naming](../../../../.agents/skills/go-naming/SKILL.md)。

## 验证

对改动文件运行 gofmt；执行 `go build ./internal/...` 与 `go vet ./internal/...`，
`cmd/` 下各个独立 main 文件分别构建到临时输出，避免多个 main 一起编译。
CLI 语法改动保留能锁定语法行为的回归测试；live 验收按用户范围和可用隔离环境开展。

当前失败需要本机归因，不能把旧的 build / vet 告警直接当作豁免；Go 专项验证不替代同批 Python 改动的门禁。
