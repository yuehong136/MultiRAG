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
| 数据库 schema/数据迁移文档与 MySQL/Peewee 工具 | `docs/database-migration.md`、`api/db/schema_bootstrap.py`、`configs/alembic/`、`docker/migration.sh` | 按 PostgreSQL/SQLAlchemy/Alembic 与本地六卷归档能力适配；模型建表、迁移版本、业务数据和完整恢复分别核验，不复制上游工具或初始化开关，见 [数据库迁移指南](../database-migration.md) |
| `api/apps/restful_apis/task_api.py`、已退役的旧 Canvas cancel | `api/apps/restful_apis/task_api.py` + `api/db/services/task_cancellation_service.py` + `core/utils/task_runtime.py`；Go `internal/handler/task.go` + `internal/service/task.go` | SQL 文档/图任务与可信 Agent/DataFlow 运行登记共同授权；Python/Go 共用 Redis v1 归属、CAS、24 小时 TTL 及 SQL 取消标记。Web POST 迁移已验收，旧 PUT 路由及独占文件已删除；POST/PATCH 保留，不增加 GET，见 [HTTP API](../references/http_api_reference.md#task-api) |
| `rag/` | `core/` | 只做语义移植，不按目录名机械复制 |
| Python 内置 embedding 与 FastEmbed 退役 | `core/llm/embedding.py` 的 `DefaultEmbedding` / `BuiltinEmbed` | BAAI 保留 FlagEmbedding/PyTorch 进程内推理，Builtin 保留 TEI。FastEmbed 依赖与注册退出，旧租户配置明确报迁移错误、不静默换向量；独立质量评测使用固定 BAAI 权重和新索引。见[进程内 embedding](../references/in-process-embedding.md) |
| Skills Go服务、Python文件互调、内嵌Web与skill_hub CLI | Go `internal/{entity,dao,service,handler}/skill_*.go`；Python `api/skills/core_*.py` + `skill_core_api.py`；独立Web `/skills`；CLI `contextengine/skill.go` | Go保留源码结构，Python原生实现相同核心行为；独立数据库、对象和索引。固定 `/skill-core` 消费空间与Files版本，必须指定真实空间。Python `/skill-assets` 保留不可变发布、持久任务和严格rerank，MCP可选只读分发。两端不共用私有schema，不依赖Python→Go转发，不执行技能。见[运行边界](../skills/README.md)。 |
| PaddleOCR 算法、同步 / 云 Job 服务与模型选择弹窗 | `deepdoc/parser/paddleocr_parser.py`、`core/llm/ocr_model.py`；独立 Web `settings/model-providers` | 保留模型 API 的嵌套 JSON 和环境配置；按 OCR / 布局算法构造请求与解析结果，官方 Job 使用上传、轮询和 JSONL。四算法、完整 URL 与令牌合同见 [PaddleOCR](../../deepdoc/parser/PADDLEOCR.md) |
| `rag/llm/chat_model.py` 与 Agent 工具历史 | `core/llm/chat.py` + `core/llm/chat_model/`，`agent/tools/base.py` 与 `agent/component/agent_with_tools.py` | 正式 DeepSeek 注册走 `LiteLLMBase` 异步入口，包内保留同步模型入口；共用带类型的工具历史，按轮保留 DeepSeek reasoning、多工具和失败结果，其他 provider 展示及可信 MCP 分派保留。见 [聊天模型与工具历史](../../core/llm/README.md) |
| connector slim inventory 与 deleted-file sync | `common/data_source/`、`core/svr/sync_data_source.py`、`api/db/services/connector_service.py`、`document_service.py`；独立 Web 数据源配置 | Airtable 附件、Google Drive 身份清单、Bitbucket PR 与 Gmail 线程已接入共享删除入口；全量无时间窗口、分页/权限/不完整搜索失败阻断、成功空清单清空、Notion 限定明确根递归枚举、KB/connector 历史 ID 原位兼容；保留本地文件过滤、事务及提交后存储清理边界，见 [连接器删除同步](../../common/data_source/README.md) |
| Go Google provider、模型级 thinking 与 sender 错误合同 | `internal/entity/models/google.go`、`internal/entity/model.go`、`internal/handler/providers.go`、`internal/service/model_service.go`、`configs/models/*.json` | 真实 genai 文本/分页/取消；region BaseURL 与模型默认、显式 false、安全失败。保留本地 MiniMax 推理和未改 provider fallback；Python 另读 `llm_factories.json`，仅必要 LiteLLM 同 delta reasoning/content 两 loop 适配，Go JSON 不自动影响 Python。见 [Go Provider API](../references/http_api_reference.md#go-provider-api并行实现) |
| Go 模型驱动与租户模型绑定 | `internal/entity/models/` + `internal/service/model_service.go`，检索/NLP 与 chat session 共用绑定驱动，`ModelBundle` 已退出 | 已移除 `internal/service/models` 第二工厂；绑定模型名、凭据和 region/base_url，实例独立 driver 不污染全局；自定义多能力模型声明、Extra.model_types 持久化与 active/inactive 列表复用现有 ModelType（首项兼容旧 model_type）；实例模型 DELETE/CLI DROP MODEL 按租户事务删除，实例删除清理其模型，失败整批回滚，vLLM 提供标准文本/历史/SSE/发现；兼容 `model@provider`、`model@instance@provider` 与旧 tenant_llm APIBase。禁用记录和 SQL 错误不降级；Google/Aliyun 能力与模型 thinking 默认保留。模型聊天及 CLI 使用 `/api/v1/chat/completions`，body 传 provider/instance/model/messages（兼容旧 message）；驱动 ChatWithMessages 返回正文/推理结构，非流式 text/image_url content 贯通 provider 与 session，文本流式保留完整历史、多模态流式明确拒绝；保留布尔值缺省与显式 false。SiliconFlow embedding/rerank、VolcEngine Ark 文本/推理 SSE/模型列表、Moonshot/MiniMax 文本/历史聊天与推理 SSE 见 [Go Provider API](../references/http_api_reference.md#go-provider-api并行实现)；Go session 使用完整角色历史与 APIConfig/context 的 sender，保留 JSON/SSE 和错误取消，不迁移 Python 模型架构 |
| Go 模型系列与能力类型 | `configs/models/`、`internal/entity/model.go`、`internal/entity/models/common.go` 与 `ChatConfig.ModelClass` 消费者 | `class`/`ModelClass` 表达模型系列；`model_types`/租户 `model_type` 表达能力。系列按模型覆盖、provider 默认、去命名空间的名称推导，小写匹配且不改请求模型 ID；Gitee/SiliconFlow 共用解析结果，空异步 suffix 不切换端点。见 [Go Provider API](../references/http_api_reference.md#go-provider-api并行实现)，当前 Web 继续消费 Python 能力合同 |
| Go embedding Encode 与查询消费者 | `internal/entity/models/`、`internal/service/nlp/retrieval.go` | driver 统一四参数 `Encode`，绑定模型保留单参数批量 `Encode`；检索查询使用单项批次；旧 ModelBundle 及无消费者的 token 估算已退出。绑定边界共用数量、非空、维度和有限数值校验；保留 provider 协议、旧凭据、region 和明确不支持的能力。见 [Go Provider API](../references/http_api_reference.md#go-provider-api并行实现) |
| `rag/svr/task_executor.py` 的 TOC executor 生命周期 | `core/svr/task_executor.py` 的 `do_handle_task` / `_shutdown_toc_executor` | 仅 TOC 按需创建自有线程池，提交与所有后续出口处于受保护生命周期；异步等待和事件循环外 join/排空先于 session 释放、取消清理及 worker ack。运行线程等待完成，共享池保持可用，source 状态继承和母块保护不变 |
| `agent/component/list_operations.py` | 同名组件 + `api/db/services/canvas_service.py` 运行适配 | 新合同显式 `operations_version: 2`，缺省/1 保留历史 topN 与 head/tail 单项语义；只在运行副本补版本，不批量改写存储。错误经普通或 OpenAI 协议传递，见 [HTTP API](../references/http_api_reference.md#列表操作组件与历史-dsl) |
| `deepdoc/parser/pdf_parser.py` 的 bbox 分批与布局选择 | 同名 parser、`deepdoc/vision/layout_recognizer.py`、`common/deepdoc_config.py` | 类型化读取 `deepdoc` section，保留上游环境名；窗口内裁图，返回全局页码。flow 多栏排序读 `bbox_page_width`。DLA 可选客户端协议未提供，缺客户端时明确失败，不复制成功桩 |
| `api/apps/kb_app.py` 的数据集管理、图谱和 RAPTOR 路由 | `api/apps/restful_apis/dataset_api.py`、`document_api.py` | `/api/v1/datasets` 提供管理与索引契约；已迁移的旧创建、更新、列表、删除、图谱读取/删除及 GraphRAG/RAPTOR 启动/追踪入口已移除。文件日志等本地扩展仍由 `kb_app.py` 提供，其他入口按实际调用逐项退役 |
| dataset/document metadata config 与 dataset ingestion logs | 同名 REST 路由、`api/apps/services/dataset_api_service.py`；真实 HTTP 回归在 `tests/integration/test_dataset_management_http.py` | 模板更新只写 metadata，保留其他 parser 配置；dataset owner 与 document owner/admin 权限分别沿用当前合同。摄取日志日期按 UTC 比较，反向范围返回业务错误；保留活动 Web metadata fallback；摄取列表通过 log_type 分流文件/数据集日志，支持关键词及文件类型/后缀筛选，Web 两个页签均消费该入口，见 [HTTP 合同](../references/http_api_reference.md#元数据模板配置) |
| `chunk_app.py` 的 retrieval_test 与文档 knowledge_graph | `api/apps/restful_apis/dataset_api.py` + `api/apps/services/dataset_search_service.py`；独立 Web `knowledge-retrieval.ts` 和 Python 管理 CLI | REST dataset search 保留联合检索、成员权限、元数据、搜索模式和禁用 chunk 过滤；graph 无 doc_id 复用聚合图，有 doc_id 保留文档子图/思维导图及数据集归属检查。实际 Web 消费者已迁移并经隔离 HTTP/SQL/Milvus 验收，两个旧 chunk 入口及专用模型已退出；其他 chunk 管理、SDK 检索及图谱兼容入口保留，见 [Dataset REST API](../../api/apps/restful_apis/README.md#数据集检索) |
| `api/apps/evaluation_app.py` 的评估路由 | 评估 API 已移除；`api/db/services/evaluation_service.py` 与评估表保留 | 未使用的 `/v1/evaluation` 已下线；知识库 REST 数据集不承接评估数据集。入口退役不自动删除 service、数据库表或历史数据 |
| `document_api.py` 批量文档状态与 source availability | `api/db/services/document_status_service.py`，共享 worker/REST/legacy source 写入；Go `internal/handler/document_status*.go` + `internal/service/document_status.go` + DAO/Infinity | dataset-scoped POST、owner/admin、每文档映射；SQL 行锁排序索引补偿与新 source 状态继承；同文档完整父子引用保护旧/新母块、ES/OS 完整响应校验及 Go 独占 Infinity 连接/个人 Principal 校验。Web 已迁至新 REST 并完成真实验收，旧 change_status 与专属模型已删除，共享状态服务保留。Go Infinity 真写，ES/Milvus 无基盘明确失败，见 [HTTP 合同](../references/http_api_reference.md#批量更改文档启用状态) |
| `api/apps/document_app.py` 的网页、空白文档创建 | `api/apps/restful_apis/document_api.py` + `api/apps/services/document_api_service.py` | 新入口由 `/api/v1/datasets/{id}/documents?type=web|empty` 提供；旧 `/v1/document` 创建路由保留 deprecated 兼容层 |
| `api/apps/document_app.py` 的文档 run 编排 | `api/apps/restful_apis/document_api.py:/documents/ingest` + `api/db/services/document_ingest_service.py`、`document_ingest_recovery.py` + 共享 Task/worker/Pipeline | 全请求预检、严格 run/delete/apply_kb、完整逐文档结果；解析历史与 SQL 计数补偿使用持久恢复材料，Task 世代约束迟到写入。canonical parse/stop 默认保留，admin 和 Web 显式重新解析已迁至 ingest 并完成真实验收；旧 run 已独立移除并完成真实 404、完整存储保全及清理验收，见 [HTTP 合同](../references/http_api_reference.md#批量提交取消或重置文档解析) |
| `api/apps/document_app.py` 的 change_parser 与文档 parser/Pipeline 更新 | `api/apps/restful_apis/document_api.py` 的 document PATCH + `api/db/services/document_parser_service.py`；`api/utils/document_parser_config.py`、`document_parser_mode.py`；共享 source/Task 写入与 `document_source_recovery.py`、`document_image_lock.py` | 严格局部配置和字段提供语义；真实模式变化一次 reset/save，同模式/仅配置/非 parser 更新不 reset，保存不入队，合法保存可改时间/行版本。DataFlow 按 KB-owner/category/权限及当前 SQL DSL 预检。native effect 前独立保存 SQL 权威恢复材料，统一锁序与完整行归属核对保全后来 winner；Redis 为镜像，unknown 不隐式重放。API、SDK 与 Web document PATCH 消费均已接受；旧 change_parser 已独立移除，真实 global404、零写与恢复回归已完成根接受，见 [HTTP 合同](../references/http_api_reference.md#更新文档解析配置) |
| `api/apps/document_app.py` 的 thumbnails 与 image 读取 | `api/apps/restful_apis/document_api.py` + `api/apps/services/document_image_http.py` + `api/db/services/document_image_service.py` | 三个可信异步 GET 提供 filtered thumbnails、KB登记图片和 owner runtime 附件；完整 raster bytes/MIME、安全局部错误和 no-store/nosniff，列表 producer 使用 encoded canonical URL。旧 thumbnails JSON 与 binary 入口均已移除并返回 routing404；Agent Hub、Web 图片核心及 Agent history 认证保存/reload 图片链均已接受，见 [HTTP 合同](../references/http_api_reference.md#读取缩略图和图片) |
| `api/apps/document_app.py` 的聊天附件 `upload_info` | `api/apps/restful_apis/document_api.py:/documents/upload` + `FileService.upload_infos` | 重复 multipart `file` 或 query `url`，对象/数组响应，owner 由异步 Principal 确定；旧 `/v1/document/upload_info` 已随 web 迁移验收完成而移除，SDK `/files/upload_info` 保留字段 `files`。共用上传、可信描述登记与失败补偿；MCP ID 按当前 owner 恢复并解析后进入模型输入；浏览器经 `common/safe_crawl.py` 校验/绑定取件。运行时元数据契约见 [HTTP API](../references/http_api_reference.md#上传运行时附件) |
| `api/apps/document_app.py` 的临时 URL/文件转文本 `parse` | HTTP 入口已移除；`FileService.parse_docs` 保留 | 无活动消费的 `/v1/document/parse` 直接退役；写作参考仍消费共享文本解析，数据集异步 `documents/parse` 独立保留，见 [HTTP API](../references/http_api_reference.md#上传运行时附件) |
| `api/apps/document_app.py` 的会话 `upload_and_parse` | 旧 route、`doc_upload_and_parse` 与专属线程 Session helper 已删除 | 当前没有活动产品消费者，会话同步解析入库能力正式退役；运行时附件上传与内容提取继续提供，其行为与会话入库不同。Web 死方法/专用测试已清理，两页附件状态由生产 hook 单独管理，真实取消/迟到/取件验收完成；共享文件上传/解析、dataset 任务与 source 状态继承保留，见 [HTTP API](../references/http_api_reference.md#上传运行时附件) |
| `api/apps/document_app.py` 的沙箱产物下载 | `api/apps/restful_apis/document_api.py` + `api/apps/services/sandbox_artifact_service.py` + `core/utils/sandbox_artifact_registry.py` | CodeExec 生成带 `run_id`、可选 `session_id` 的 REST 链接并登记精确文件归属；旧 `/v1/document/artifact/{filename}` 保留 deprecated 兼容层，两者共用登记核验 |
| `api/apps/restful_apis/openai_api.py` 的聊天补全 | `api/apps/restful_apis/openai_api.py` | 新入口为 `/api/v1/openai/{chat_id}/chat/completions`；旧 `/api/v1/chats_openai/{chat_id}/chat/completions` 由同一 handler 保留 deprecated 别名，待客户端迁移后退役 |
| `api/apps/backward_compat.py` 的旧聊天、会话、文件与 chunk 请求 | 现有 `restful_apis/chat_api.py`、`openai_api.py` 与 `sdk/session.py` | 会话 PUT 已退出，滞后文档已修正为 PATCH；SDK question/industry 合同与现有 OpenAI 别名保持，不新增重复网关。无消费者的旧 file、chunk PUT、chat_id 删除请求不恢复；详情与退出条件见 [REST README](../../api/apps/restful_apis/README.md#聊天会话兼容) |

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

Web execution 的上下文准备与来源快照位于 `api/db/services/agent_execution_service.py`，发布/开发
授权位于 `api/identity/mcp_delegation/`；`AgentExecutionOrigin` 是 additive 自有表。同步 MCP/Canvas
变更时保留这两个外层接缝。Agent 的 interaction composition 只读取 provider 的 capability，不增加
Web/Channel/草稿/发布权限判断；退出条件仍是上游提供等价 context/interceptor 接口。
当前授权语义见 [EIM-ADR-28](DECISIONS.md#eim-adr-28web-发布运行与只读草稿开发授权分离)。

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
