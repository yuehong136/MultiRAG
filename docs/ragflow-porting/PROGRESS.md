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

## a9e5724b46e9f006b90ddd70f812fb59840c6806 · 统一文档创建入口

- 上游：`infiniflow/ragflow` #14345，提交于 2026-04-27；目标父提交是上项 `4dcc42e0`。
  2026-09-27 fetch 后 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`；
  本项按目标提交的 10 文件完整 diff 评估。

| 上游 diff | 本项结论 |
|---|---|
| `api/apps/restful_apis/document_api.py` 新增 `type=local|web|empty` | 移植到现有 FastAPI `POST /api/v1/datasets/{id}/documents`。`local` 保留已有 `file`/`files` 批量上传与返回数组契约；`web` 接收表单名称和 URL，校验租户与 URL 后转换 PDF，复用现有 `FileService.upload_document`；`empty` 接收 JSON 名称，创建虚拟文档与文件关联。网页的 Selenium 与同步存储链在自有会话的工作线程执行；空白文档经请求会话桥接遗留同步 service。 |
| `api/apps/document_app.py` 删除旧 `/web_crawl`、`/create` | 本地仍有旧调用方，两个路由保留可用并标记 deprecated，待消费方迁移后退役。 |
| `docs/references/http_api_reference.md` 的三种请求示例 | 更新本仓同名参考文档和 REST 模块 README；按本地契约说明 `file`/`files`、返回形状和不自动解析。 |
| `test/testcases/test_web_api` 四个文件的 helper/验收改写 | 不复制上游 Quart/Peewee harness；本仓增加 FastAPI 路由、鉴权、URL 阻断、空白文档与文件关联等单元回归，保留已有上传回归。 |
| `web/src` 三个文件的调用切换 | 本地前端在独立 `../web` 仓，本项不改该仓；服务端新路径已就绪，旧路径供其迁移期间使用。 |

后续链核对：`a339e8a57` 处理批量文件部分成功，属于后续上传契约变更；
`6e0e49592` 修复阻塞式上传/网页处理，这里已经按 MultiRAG 异步边界将网页链放入线程，
两条后续提交均不作为本项额外范围。现有 `is_valid_url` 检查输入 URL 的出网地址；
浏览器抓取过程仍依赖既有 Selenium 实现。

验证：`make verify` 通过（8 条 import contracts、mypy 124 个源文件、unit 3115 passed）；
新增创建模式与既有上传路由定向测试通过，另增 OpenAPI 兼容标记检查。
`make integration` 因 Redis `127.0.0.1:6379`、MinIO `127.0.0.1:9020` 不可达而未运行测试；
`make smoke` 因 API `127.0.0.1:8123` 未启动而失败。本项未启动会初始化配置数据库的服务入口，
因此当时真实存储写入与网页抓取端到端验收尚未完成。

补验收（2026-09-27，基础服务启动后）：`make integration` 通过 235 个测试。
API 使用仅供本次验收的新建 PostgreSQL scratch 库启动，`make smoke` 通过；健康检查的
数据库、Redis、文档引擎和存储状态均为 `ok`。通过真实 REST 请求创建了 `empty` 虚拟文档、
`local` 文本文档和 `web` PDF 文档，逐个通过列表接口读回，并独立查验 scratch 库中的
文档行和文件关联。删除文档、读回空列表、删除数据集后，API 停止且 scratch 库已删除；
未对配置的 `xldu` 库运行启动迁移。公网域名在本机被 DNS 映射到保留地址而被 SSRF 校验拒绝，
网页模式改用通过校验的 `https://1.1.1.1/cdn-cgi/trace` 完成验收。

补验收发现 webdriver-manager 在本机将可执行路径指向 `THIRD_PARTY_NOTICES.chromedriver`，
导致网页创建返回业务错误。已在共享 `html2pdf` helper 中选择同目录真实 `chromedriver`，
并补齐其执行权限；增加对此缓存布局的回归测试。修复后网页创建成功并完成上述读回与清理。
修复后重跑 `make verify`（unit 3116 passed）与 `make integration`（235 passed），均通过。
