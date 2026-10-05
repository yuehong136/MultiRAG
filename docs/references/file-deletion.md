# 文件批量删除合同

## 通用文件

`DELETE /api/v1/files` 接收 `{"ids":["file-or-folder-id"]}`，至少一个 ID。
Web JWT 和 API token 使用同一鉴权入口。HTTP 200 必须同时检查业务码：

```json
{"code":102,"message":"Deleted 1 files with 1 errors","data":{"success_count":1,"errors":["File or Folder not found: missing-id"]}}
```

- 完全成功 `code=0`，任何单项错误（含全部失败）`code=102`；两者都有
  `data.success_count` 和 `data.errors`，成功的 `errors` 是空数组。
- `success_count` 是本请求实际完整删除的唯一 File 记录数，**含递归子文件和目录**，
  不等于请求 ID 数。重复 ID、父子重叠只处理/计数一次。
- `errors` 为诊断字符串数组，不是稳定的错误码/资源 ID 映射；消费者不得解析字符串
  驱动补偿，也不能用它精确推导失败选择集。
- 单项不存在、缺 tenant、无权限、对象/文档/关系/文件记录删除失败均继续其他合法项。
  递归对子项分别鉴权；失败子项保留，祖先目录也保留，其他兄弟继续删除。
- 文件沿用 owner/team 权限；有关联文档时，**全部关联 dataset 权限先校验**，才开始
  对象删除。缺文档或关联 dataset 无权访问时，保留该文件及对象。
- `source_type=knowledgebase` 的 File 不能从此入口删除，返回错误，调用者应使用
  dataset documents 接口。它不会静默跳过并宣称成功。

每个普通文件的处理顺序是：文件/关联文档权限预检 → 原对象删除 → 关联文档删除
（文档自管 SQL 事务、取消任务、缩略图/对象、索引/元数据/图谱清理）→ 残余关系删除及
读回 → File 行删除。目录在所有子项成功后删除。数据库删除行数必须为 1；关系必须
读回为空。对象适配器的删除异常向上传播，显式 `False` 也视作失败。

文档删除使用 `DocumentService.remove_document(..., strict=True)`；该模式报告其捕获到的
清理异常和元数据删除 `False`。其他文档调用方保留既有默认 best-effort 行为。
Redis 取消 helper 的历史内部重试/吞错语义没有在本合同改成强交付保证。

## 一致性、重试与服务入口

此操作是尽力完成的批处理，**不是跨 SQL/对象/索引的原子事务**。失败前已提交的文档
记录/关系删除、已删除的对象或索引不会回滚。因此错误不代表无副作用，失败的文件
保留也不代表其关联文档/原对象仍完整。

同一请求内去重；后续请求中的不存在 ID 返回错误，不静默视作幂等成功。对象已不存在
但存储后端确认删除成功时可继续。没有持久化的清理任务账本：文档 SQL 已提交后发生的
索引清理失败需要独立核查/修复，不能只重发 File ID 就保证补齐原文档的清理。

同进程消费者使用
`api.apps.services.file_api_service.delete_files_async(uid, file_ids)`，返回
`(success, {"success_count": int, "errors": list[str]})`。它在线程中自开同步 session。
`delete_files(db, uid, file_ids)` 是同步编排；内部依赖有自己的 commit/rollback，
不要将它包进期待原子性的外部事务，也不要共享 session 给并发任务。

Skills Space 应在自己的持久 operation 中维护授权后的资源清单、文件绑定和索引步骤，
按真实读回决定重试；受管目录保护、版本/安装状态与索引协调归 Skills 领域。禁止直接
删 File 行、绕过鉴权递归，或用返回 HTTP 200/部分成功表示整个 space 卸载完成。

## Web 消费合同

当前独立 Web 仓没有通用 `/files` API、文件管理 hook 或文件管理页面，知识库文档页
使用 `/datasets/{id}/documents`。不把该页面替换成通用文件批删。

后续文件/Skills 页面无论完全成功、部分失败或网络结果不确定，都应 invalidate 当前
文件列表并重新读取；仅完全成功显示删除成功。保留当前页和过滤条件；刷新后按新的
`total` 将页码收敛到 `max(1, ceil(total/page_size))`，当前页仍有效时不跳回第一页。
保留刷新后仍存在的选择项，失败反馈使用固定国际化文案，不渲染服务端原始错误。
删除包含递归项时，不能从 `success_count` 直接扣减当前页的 total。

## 验证范围

回归入口为 `tests/unit/test_file_batch_delete.py`、`test_file_restful_api.py`、
`test_file_api_service.py`、`test_document_service_delete_transaction.py` 和
`test_storage_delete_errors.py`。真实 HTTP 验收为
`make integration TESTS=tests/integration/test_file_batch_delete.py`：临时 PostgreSQL
数据库、专用 MinIO 桶、Milvus 集合和真实 JWT，独立连接读回 SQL/关系/计数，物理列举
对象、读回索引和 HTTP 文件列表，验证故障后重试与资源清理。

真实后端验收覆盖 PostgreSQL/MinIO/Milvus；S3 故障传播有单元测试；OSS/GCS/Azure
未做真实云端验收。Web 暂无对应页面，刷新/分页规则尚未进行浏览器验收。

## Dataset 文档删除的整批预检

`DELETE /api/v1/datasets/{dataset_id}/documents` 保留独立合同：
`ids` 与 `delete_all=true` 必须提供其一，非空 `ids` 与 `delete_all=true` 互斥。
先检查 dataset 可访问性，再将整批 ID 与该 dataset 的文档集合比较；包含任一不存在、
同 owner 的其他 dataset 或其他 owner 的文档 ID，整批 `code=102`，不进入删除链。
此预检拒绝没有文件/对象/关联文档/索引/任务/元数据/计数的删除副作用。

合法 `delete_all=true` 仅使用当前 dataset 的 ID 集合；空 dataset 返回
`{"code":0,"data":{"deleted":0}}`。重复指定 ID 在预检后去重。
文档 ID 是字符串，允许 connector 的非 UUID 标识；不存在 ID 由存在性/范围预检拒绝，
不依靠 UUID 格式判定权限。重复 ID 只删除、计数一次；这个合同不改变其他请求的 UUID 校验。
该预检合同不等于后续合法请求中的跨存储删除原子性保证。

Web 文档页通过 `useDeleteDocument(datasetId)` 调用该 REST 入口。现有回退
`POST /v1/document/rm` 接收 `doc_id: string[]`，也接受非 UUID 并去重；它没有 dataset
参数，逐项校验文档存在、dataset 有效、用户属于 dataset 的 `tenant_id` 且角色为
owner/admin/normal，全部通过才删除。不能用 `created_by` 代替所属 tenant。
任一项无权限或不存在时整批 `retcode=109`；成功仍为 `code=0,data=true`。

旧 `DELETE /v1/dataset/{dataset_id}/documents/{document_id}` 先校验 dataset 权限，
再验证文档的 `kb_id` 与路径 dataset 一致，之后才初始化文件目录或进入删除服务。
无权限为 HTTP 403；不存在或错 dataset 为 HTTP 404。含 `/` 的 ID 使用 REST JSON
批删入口，此旧单路径参数不提供斜杠 ID 合同。

`tests/integration/test_document_delete_scope.py` 使用真实 JWT/API-key 请求，比较拒绝前后的
完整 SQL、物理对象字节、Milvus payload/向量以及专用 Redis 队列；并读回合法
`delete_all`、非 UUID 重复 ID 删除后的目标清理与其他 dataset 保留，包含创建人与
所属 tenant 不同、无效 dataset 和两个兼容入口。现有路由单测
`tests/unit/test_restful_document_delete_route.py` 覆盖归属、去重、互斥与业务码。
