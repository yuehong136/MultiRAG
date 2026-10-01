# RAGFlow 上游移植映射与本地兼容层

本文是 `port-ragflow-commit` 工作流引用的入库映射。它只记录稳定的路径所有权、框架翻译和本地
兼容层退出条件；单次上游提交结论仍记录在对应任务账本，不在这里追逐 `main`。

## 1. 配置与框架映射

| RAGFlow 上游 | MultiRAG 落点 | 规则 |
|---|---|---|
| `api/settings.py`、`rag/settings.py` | `common/app_config.py` + `common/resources.py` | 新配置进入类型化 `AppConfig`；状态资源由 `resources` 懒加载 |
| 消费者的 `settings.X` | `common/settings.py` | 上游同步文件继续照抄访问面，由永久 facade 翻译；不要逐文件改成 DI |
| Quart/Flask handler | FastAPI router + Pydantic + dependency | 新 service async-first；同一请求不混用同步/异步 session |
| peewee model/query | `api/db/db_models.py` + SQLAlchemy 2.0 repository | 迁移和真 PostgreSQL 测试固定约束；业务事务放自有 service/repository |
| `rag/` | `core/` | 只做语义移植，不按目录名机械复制 |
| `deepdoc/parser/pdf_parser.py` 的 bbox 分批与布局选择 | 同名 parser、`deepdoc/vision/layout_recognizer.py`、`common/deepdoc_config.py` | 类型化读取 `deepdoc` section，保留上游环境名；窗口内裁图，返回全局页码。flow 多栏排序读 `bbox_page_width`。DLA 可选客户端协议未提供，缺客户端时明确失败，不复制成功桩 |
| `api/apps/kb_app.py` 的数据集管理、图谱和 RAPTOR 路由 | `api/apps/restful_apis/dataset_api.py`、`document_api.py` | `/api/v1/datasets` 提供管理与索引契约；已迁移的旧创建、更新、列表、删除、图谱读取/删除及 GraphRAG/RAPTOR 启动/追踪入口已移除。文件日志等本地扩展仍由 `kb_app.py` 提供，其他入口按实际调用逐项退役 |
| `api/apps/evaluation_app.py` 的评估路由 | 评估 API 已移除；`api/db/services/evaluation_service.py` 与评估表保留 | 未使用的 `/v1/evaluation` 已下线；知识库 REST 数据集不承接评估数据集。入口退役不自动删除 service、数据库表或历史数据 |
| `api/apps/document_app.py` 的网页、空白文档创建 | `api/apps/restful_apis/document_api.py` + `api/apps/services/document_api_service.py` | 新入口由 `/api/v1/datasets/{id}/documents?type=web|empty` 提供；旧 `/v1/document` 创建路由保留 deprecated 兼容层 |
| `api/apps/document_app.py` 的沙箱产物下载 | `api/apps/restful_apis/document_api.py` + `api/apps/services/sandbox_artifact_service.py` + `core/utils/sandbox_artifact_registry.py` | CodeExec 生成带 `run_id`、可选 `session_id` 的 REST 链接并登记精确文件归属；旧 `/v1/document/artifact/{filename}` 保留 deprecated 兼容层，两者共用登记核验 |
| `api/apps/restful_apis/openai_api.py` 的聊天补全 | `api/apps/restful_apis/openai_api.py` | 新入口为 `/api/v1/openai/{chat_id}/chat/completions`；旧 `/api/v1/chats_openai/{chat_id}/chat/completions` 由同一 handler 保留 deprecated 别名，待客户端迁移后退役 |

## 2. EIM-U14 Interaction 所有权

| 路径 | 所有权 | U14 允许的改动 | 上游出现等价能力后的退出条件 |
|---|---|---|---|
| `common/mcp_tool_call_conn.py` | RAGFlow 同步冲突岛 | 只保留 SDK2 typed outcome、resume 参数和注入 Protocol；不得导入 `api` 或数据库 | 上游提供等价 per-call MRTR outcome 后收敛到上游类型 |
| `agent/component/agent_with_tools.py` | RAGFlow 同步区 | 最多一个 composition seam；不含持久化、租约、Provider 或授权分支 | 上游支持 tool-call interceptor/context injection 后删除本地 seam |
| `agent/canvas.py`、`core/llm/*` | RAGFlow 同步区 | U14 默认不修改；暂停通过控制信号在外层适配，不复制或分叉 Agent loop | 上游原生 run/checkpoint 只在语义审计后接入，不并行维护本地 checkpoint |
| `common/mcp_interactions.py` | MultiRAG 自有反腐层 | transport-neutral DTO、canonical digest、无敏感信息控制信号 | 上游类型能完整表达本项目安全绑定时可缩减 |
| `api/identity/mcp_interactions/` | MultiRAG 自有 | Interaction 状态机、加密、repository、恢复 lease、重授权 port | 只在上游提供同等 tenant/principal/policy/lease 语义时评估删除 |
| `api/db/db_models.py` + Alembic | 共享模型文件 + MultiRAG 自有表 | 仅 additive model；表名和约束不依赖 Canvas/LLM 内核 | 上游若提供通用 interaction ledger，先迁移/等价验证再退役 |
| `api/channel_*` / Provider renderer | MultiRAG 自有 | U14 不修改；U15 才接飞书 renderer | 不随 RAGFlow Canvas 提交回卷 Provider 语义 |

## 3. 每个上游 commit 的 U14 回归判据

移植涉及 MCP、Agent、Canvas、chat model 或数据库模型时，除常规 `make verify` 外，必须检查：

1. `InputRequiredResult.requestState` 不进入模型历史、日志、trace 或 Provider payload；
2. pause 不触发下一轮 LLM，也不重放 renderer；
3. resume 仍绑定 tenant、Principal、published agent revision、server/resource/tool 和原参数 digest；
4. 每轮恢复重新授权并换发 bearer，transport/session state 不成为 authority；
5. `side_effect` 的未知结果不自动重放；
6. pause 时捕获的工具 `outputSchema` 仍与 published agent/tool 调用绑定，恢复结果必须由 Host 再验证；
7. 上游新增原生 MRTR/checkpoint 时，先比较上述不变量，再决定直接跟进、语义移植、适配层吸收或暂不采纳。

## 4. 当前动态对照

- 2026-08-22 只读刷新：`infiniflow/ragflow` `origin/main` =
  `f796721ff25f0f86e4499c166f0c49228d5f6ad7`。
- 该 SHA 只用于 U14 冲突面审计，不改变 EIM-F5 / CHN-X14 的挂起状态，也不代表本地逐 commit
  同步进度。
- 相对 2026-08-13 定向快照，U14 相关显著变动仍集中在 `agent/canvas.py`、
  `rag/llm/chat_model.py` 和 `api/db/db_models.py`；因此本任务不把 Interaction runtime 写入这些
  上游执行内核。
