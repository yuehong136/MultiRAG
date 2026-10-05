# Connector 文档删除核对（2026-10-05）

目标提交 `05ee7f8bb68836b099bd99eac079ad5e1e5bc544`，上游 checkout
`/Users/xldu/project/ragflow`，remote 为 `infiniflow/ragflow`。仅使用冻结上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f` 内的历史；目标是该上限的祖先。
指定 diff 包含 DeleteDocumentReq 及 HTTP/SDK/Web 三类测试；文档删除取消 UUIDv1
格式校验，仍拒绝重复 ID。后续历史没有回退这一修复；冻结上限包含后来 Python
源码退役（`670e68872`），不用于替换本地生产 FastAPI。

## 等价部分与本地适配

canonical `DeleteDocumentsRequest.ids` 已为 `list[str] | None`，没有 UUID 校验。
`delete_documents` 先校验 dataset 访问权限、ids/delete_all 互斥、完整批次的存在性与
dataset 归属，才进入 `FileService.delete_docs`。这部分保持原实现；不修改通用 UUID
校验器，也不放宽 dataset、chunk 等其他请求。

本地重复 ID 的合同是预检后去重，返回唯一删除数，已有
`test_restful_document_delete_route.py::test_delete_deduplicates_ids` 固定行为。
不照搬上游的重复 ID 参数拒绝。新验收使用非 UUID connector ID（含 `/`），重复发送
合法 ID 后验证 `deleted=1`，避免重复计数和第二次删除的 not-found 假失败。

本次修复两个已注册的兼容入口：

- `POST /v1/document/rm`：按 dataset 的 `tenant_id` 而非 `created_by` 查询有效
  owner/admin/normal 成员，并要求 dataset 有效；全批预检后只删除唯一 ID。
  此入口无 dataset 参数，允许删除多个有权限 dataset 的文档。
- `DELETE /v1/dataset/{dataset_id}/documents/{document_id}`：先校验 dataset 权限、
  文档存在及 `kb_id`，再调用统一文件删除服务。原先在预检前初始化目录、把根目录
  dict 当作 ORM 对象访问，以及用对象存储地址判断 dataset 的路径均移除。

旧 Web 删除函数的说明同步删除了与实现不符的字符串输入、跨存储原子性等承诺。
稳定接口合同见 [文件删除合同](../references/file-deletion.md)。

## 实际消费者

- Web：`use-document-actions.ts` → `useDeleteDocument(datasetId)` →
  `knowledgeAPI.document.delete` → `deleteDatasetDocuments`，REST JSON 传递原始 ID；
  路由缺失时回退 `deleteDocumentsLegacy`，不在客户端做 UUID 校验。
- 指定提交的 Python SDK：`DataSet.delete_documents` → `Base.rm` →
  `RAGFlow.delete` → `DELETE /api/v1/datasets/{id}/documents`；业务码非 0 抛异常。
- 目标树 `api/apps/sdk/doc.py` 不再重复注册删除文档路由，API key 与 Web JWT
  共同进入 canonical 删除处理器。
- 两个独立 SDK 工作树 `multirag-python-sdk-v1@ed38de7f`、
  `multirag-rest-first-python-sdk@d0d34818` 的 `DocumentsManager.delete` 使用
  `/api/v1/datasets/{id}/documents/{document_id}`；目标后端无这个 DELETE 路由。
  它们的 `_id` 只检查非空并 URL 编码，问题是路由版本不匹配，不是 UUID 限制。
  本任务不修改这些独立工作树或新增一套 SDK API。

## 本机证据

隔离测试使用临时 PostgreSQL 数据库、专用 MinIO 桶、
Milvus 集合与 Redis 队列；不会操作业务数据。覆盖合法/不存在 ID 两种顺序、标点
字符串、同 tenant 错 dataset、跨 tenant、无效 dataset、互斥参数及重复合法 ID。
每次拒绝通过独立 SQL/对象/索引/队列读回比较完整快照；成功后检查目标文档、关系、
元数据、任务、计数、对象和索引清除，其他 dataset 完整保留。

- 删除集成矩阵 **5 passed，零 skip**，报告 `.test-results/20261005-141131-36407/`。
  完整逐请求响应和前后快照位于 `.test-results/document-delete-readbacks-20261005/`，
  `summary.json` 汇总 **45 个拒绝请求全部无变化**；5 组资源 manifest 均确认清理完成。
- 冻结目标 SHA 的 SDK 单独打包至 `/tmp/multirag-delete-sdk.kbfaO7/`，使用临时
  pytest 调用适配器让同一存储矩阵经真实 `DataSet.delete_documents` 发出请求，
  没有 mock SDK HTTP 传输或后端服务。结果 **1 passed**：11 次 `code=102` 正确抛异常，
  重复合法 ID 删除与随后空库 delete_all 各成功一次。读回与 SDK 业务码证据位于
  `.test-results/document-delete-frozen-sdk-final-20261005/`，资源清理完成。
- 删除相关单测 **23 passed**；保留原“只执行一次文档 DB 删除、不重复删除关系”的
  断言，按新预鉴权链补充 fixture。Web `knowledge-rest.test.ts` **11 passed**
  （`tsx --tsconfig tsconfig.app.json --test`）；这属于消费者合同验证，不是浏览器验收。
- `make smoke` 通过现有 8123 服务的 ping/healthz；变更路由由上述加载本次源码的隔离
  HTTP 服务验收，未将常驻服务健康检查当作代码部署证明。
- `make verify` 最初通过（5491 单测）；补上旧单文档入口后，最终重跑被并行改动
  `tests/integration/test_rss_deletion_boundary.py` 的格式检查阻断，之前一次阻断文件为
  `core/prompts/generator.py`。本任务 6 个 Python 文件的 Ruff 检查、全库 Ruff lint、
  8 条 import 合同、异步门禁和 mypy 138 文件通过。补跑全库单测当时为 5585 passed、
  10 failed、5 errors，其中本任务旧 fixture 的 1 项失败已修正并在上述 23 项中复验；
  其余为 KB prompt、OpenAI prompt_config 与并行 Infinity 测试错误，未修改。
- 完整核心 `make integration` 运行报告为 `.test-results/20261005-140256-22948/`：
  已执行 call 阶段 **322 passed、1 failed**，包含本次全部 5 组删除矩阵通过。
  失败是 `test_agent_execution_origin.py:179` 将迁移 head 硬编码为 `b0d2e4f6a8c0`，
  实际为 `d6f8a0b2c4e6`，与本次删除改动无关。确认目标矩阵通过后主动中止扩展运行，
  未完成余下核心用例；中断时 Milvus fixture 的快照查询报告 collection 未加载，
  runner 最终退出码 1。不能将此报告视作全套集成通过；目标矩阵独立完整运行的
  5 passed 与 45 次逐请求读回才是本次删除交付证据。

非 UUID ID 为隔离环境构造，未连接外部 connector。未验证生产部署、浏览器 UI、
其他数据库/云对象后端，也不提供跨存储原子性或并发
归属变更的保证。含 `/` 的 connector ID 通过 canonical/旧 Web JSON 删除；旧单文档
路径只验证不含 `/` 的非 UUID ID。
