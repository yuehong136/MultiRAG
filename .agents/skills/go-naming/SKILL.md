---
name: go-naming
description: 在新增或重命名 Go 标识符、包或文件时检查命名约定。
---

# Go 命名

沿用当前包和接口契约；普通逻辑修改不触发存量重命名。

- 导出标识符用 `MixedCaps`，非导出用 `mixedCaps`；首字母大小写表达可见性。
- 缩写保持一致，如 `HTTPClient`、`GetUserByID`、`userID`；常量同样用 MixedCaps / mixedCaps。
- 包名简短、小写、无下划线；优先表达职责。已有 `common`、`utility` 等包按其职责复用，不因命名建议另起目录。
- 普通 Go 文件用小写和下划线，测试为 `*_test.go`；平台文件保留 Go 工具链认可的后缀。
- 单方法接口可按能力命名为 `Reader`、`Validator`；不强制给多方法接口加 `-er`，避免 `I` 前缀和 `Interface` 后缀。
- 方法名表达实际行为，布尔谓词按上下文选择 `Is`、`Has`、`Can` 等前缀；无需给所有读取方法加 `Get`。
- 导出 sentinel error 用 `ErrNotFound` 一类命名，包内用 `errNotFound`；错误类型按职责命名。
