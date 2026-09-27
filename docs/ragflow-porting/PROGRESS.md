# RAGFlow 逐提交跟进记录

本记录只写单次提交的处理结论。稳定路径映射见
[RAGFLOW_PORTING_MAP](../enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md)；后续提交按各自任务处理。

## 4dcc42e0e14ad4a93373f08b325757cba285ac54 · 统一数据集与索引 API

- 上游：`infiniflow/ragflow` #14222，提交于 2026-04-27；核对了完整 51 文件 diff。
  2026-09-27 fetch 后的 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
- MultiRAG 主体已有提交：`a2f10eed`、`def8625c`、`b17e9a8b`。本次补齐图谱读取新路径，
  并标明旧路径的兼容状态；下一上游提交 `a9e5724b` 不在本项范围。

| 上游 diff | 本项结论 |
|---|---|
| `api/apps/restful_apis/dataset_api.py`、`api/apps/services/dataset_api_service.py` 的数据集详情、摄取概览/日志、标签聚合/修改、元数据配置及统一 `graph/raptor/mindmap` 索引 | 主体由上述三次 MultiRAG 提交语义移植；保留 FastAPI、AsyncSession、显式删除路径和旧接口兼容层。本次发现并补齐 `GET /datasets/{id}/graph/search`，复用现有图谱 service。 |
| `api/apps/restful_apis/document_api.py` 的 `POST /metadata/update`、文档 parse/stop | 已由本地 `document_api.py` 与 `document_api_service.py` 实现；混合有效/无效 ID 的 parse 响应保留非零业务码及部分执行结果。 |
| `api/db/services/doc_metadata_service.py` 的 ES 元数据整字段替换 | 已由 `metadata_store_engine.py` 等价实现：用脚本替换 `meta_fields`，避免对象深合并留下被删除的 key。 |
| `api/apps/kb_app.py` 删去旧路由与相应旧测试 | 暂不删除仍供调用方使用的 `/v1/kb` 路由；本次将上游删除范围内仍存在的旧路由标为 deprecated。新行为只在 `/api/v1/datasets` 实现，旧路由待调用方迁移后按兼容策略退役。 |
| `POST /datasets/{id}/embedding` | 暂不采纳：上游后续 `70a49c947` (#16936) 明确删除这个未使用的端点和 service。现有 `/v1/kb/check_embedding` 是抽样校验，不是该端点的等价替代。 |
| `sdk/python/ragflow_sdk/modules/dataset.py` 将自动元数据配置改用 `/metadata/config` | 本仓无该客户端 SDK 源码；服务端新路径已存在，旧 `/auto_metadata` 仍为 deprecated 兼容入口。客户端方法迁移留给 SDK 所在任务。`api/apps/sdk/dataset.py` 是服务端空 router，并非该客户端模块。 |
| `web/src/...` 九个文件的调用路径调整 | 前端是独立 `../web` 仓，按移植 Skill 另排；本次只保证新增的图谱读取路径在服务端可用，未改前端。 |
| `sdk/python/test`、`test/playwright`、`test/testcases` 的新增、改写与删除 | 不复制上游 Quart/Peewee 测试 harness；本仓现有 dataset/index、文档解析、元数据及存储回归测试覆盖已移植行为。本次扩充图谱路由契约测试和异步依赖树检查。 |

后续链核对：`35f6d81b7` (#14402) 后续扩展图谱检索 REST 行为，不能作为本项的额外实现范围；
`70a49c947` (#16936) 删除本提交新增的 embedding 入口，因此该入口不移植。

验证：`make verify` 全绿（8 条 import contracts、mypy 124 个源文件、unit 3100 passed）；
图谱新旧路径的成功/鉴权失败响应及 OpenAPI deprecated 状态由 unit 覆盖。
`make smoke` 未通过：本机没有运行中的 API，Redis 与 MinIO 不可达；本次未启动会初始化配置数据库的服务入口。
