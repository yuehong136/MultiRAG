# 文档原文件与生成附件读取

知识库文档和 DocGenerator 生成附件是两类资源，ID 不互相回退解析。

| 资源 | 路由 | 授权与存储 |
|---|---|---|
| 文档预览 | `GET /api/v1/documents/{doc_id}/preview` | 有效 Document → Knowledgebase → 当前用户的有效 tenant membership；owner/admin/normal 可读 |
| 文档下载 | `GET /api/v1/documents/{doc_id}/download` | 相同文档授权，再要求 owner/admin；读取文档登记的原文件位置 |
| 生成附件 | `GET /api/v1/documents/runtime/{attachment_id}/download?ext=markdown` | 当前用户的生成附件命名空间；32 位十六进制 ID；不查询知识库 Document |

预览按文档名确定类型，HTML/SVG 等主动内容强制附件响应。文档下载设置 UTF-8 文件名。
生成附件的 `ext` 是类型提示，未知值使用 octet-stream，不能形成任意响应头。
成功响应禁止缓存并设置 nosniff；空文件保留真实空字节，缺失对象不能伪装为成功。
跨租户和缺失文档均返回 HTTP 404、`code=102`、`Document not found!`，在读取对象前拒绝。
普通成员能预览但不能下载，下载拒绝为 HTTP 403、`code=109`。

`GET /v1/document/download/{attachment_id}` 按明确兼容承诺保留 deprecated 入口，
与生成附件 REST 路由注册同一个 handler、同一个异步 Principal 依赖与 owner 命名空间。
只有在该兼容承诺退出后才移除；不新增未被消费的 `/api/v1/document/download` 别名。
`GET /v1/document/get/{doc_id}` 已退役。Web 的预览 hook、文档查看页和下载领域 API
共同使用新合同；SDK 的既有 `GET /api/v1/documents/{document_id}` 和 dataset-scoped
下载继续保持自身鉴权合同。沙箱产物使用独立 artifact 路由及 run/session 授权。

## 上游映射

- `f703169117687dec3a6550fe21c8ea20c6497e70`：迁移 preview/download，保留本地其他
  document_app 入口；本地 metadata summary 客户端已使用 dataset REST 路由，filter 和
  summary Query 均在缺 dataset ID 时禁用。上游 API 的附件/文档混用不照搬。
- 同问题链的 `58819f5d3e1a52bded15cd0693092cc7f69aa980`、
  `71a52d579c8a588143e02ab6453112757c34f583` 后来拆分下载资源，同时曾移除 preview
  授权；`d26d7994679b1492591e47576f4db3bc9bb94e13` 恢复授权。这里只参考该资源边界，
  不迁入其余重构，也不削弱现有 owner/admin 下载或 sandbox 授权。
- `d51fb88573861e68f40d795be93101f25567b8dd`：将缺失和跨租户响应合并并在存储读取前
  检查归属；本地沿 AsyncSession 显式连接 Document、KB、有效 tenant 和有效成员，
  再复用 SDK 的登记地址与 File→folder 来源校验。停用资源、邀请成员、孤儿文档和
  非法对象路径均拒绝且不读取对象；附件不套用不存在的 Document membership。
- `d843035c8bbb9f370de2052e7aeaaf151b02833f`：保留明确要求的 `/v1` 兼容路径；
  不将其改成知识库文档下载，也不复制另一份鉴权或存储实现。

## 验证入口

`tests/integration/test_document_file_http.py` 通过实际注册 HTTP 路由、scratch PostgreSQL
与独立 MinIO bucket，检查权限、字节、header、SQL/object 读回和清理。
同文件的 `external_consumer` 用例通过 `WEB_DOCUMENT_FILE_CHECKOUT`（或
`WEB_COMPLETION_CHECKOUT`）运行 Web 生产预览 helper 与下载领域 API；它同时登记在
core 文件集合与 consumer 套件，core 默认排除该 marker，consumer 专门运行。
运行使用 `make integration TESTS=tests/integration/test_document_file_http.py`，缺服务不能
以 skip 视作通过。完整 core 与跨仓 consumer 的最终组合由批次集成者执行。
