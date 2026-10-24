# REST 覆盖索引

生产路由由 `api/apps/restful_apis/` 注册到 `/api/v1`。下表用于定位合同与行为测试，
不表示每条路由已完成真实外部服务验收；具体结果以对应提交的 JUnit、命令退出码和服务证据为准。
路径中的 `{id}` 代表相应资源 ID，测试文件相对于本目录。

| 族 | 当前入口 | 主要 unit 覆盖 | 真实服务或消费者覆盖 |
| --- | --- | --- | --- |
| Agent | `/agents`、`/agents/chat/completions`、sessions、versions、webhook logs | `test_agent_completion_async.py`、`test_agent_update_release.py`、`test_agent_webhook_restful_routes.py` | `test_agent_route_migration.py`、`test_agent_runtime_completion.py`、`test_agent_permission_matrix.py`、`test_agent_completion_close.py`、`test_agent_draft_session_persistence.py` |
| Chat CRUD | `/chats`、`/chats/{id}` | `test_chat_crud_routes.py`、`test_chat_restful_api.py` | `test_chatbot_authorization.py`、`test_agentbot_list_operations.py`、`test_rest_chat_management_contract.py` |
| Chat sessions/completion | `/chats/{id}/sessions`、`/chat/completions`、`/chat/recommendation` | `test_chat_session_compat.py`、`test_related_questions_routes.py`、`test_sdk_session_completions.py` | `test_chat_session_compat.py`、`test_runtime_chat_attachments.py`、`test_rest_chat_completion_contract.py` |
| Chunk | `/datasets/{id}/documents/{id}/chunks`、chunk PATCH/detail | `test_chunk_nullable_payload.py`、`test_chunk_content_validation.py`、`test_chunk_image_replacement.py`、`test_infinity_get.py` | `test_shared_chunk_owner_contract.py`、`test_chunk_image_replacement.py`；Infinity 读回见下文 |
| Connector | `/connectors`、dataset connectors、OAuth start/result/callback | `test_connector_restful_api.py`、`test_connector_google_oauth_pkce.py` | `test_connector_web_sync.py`、`test_connector_google_oauth_pkce.py`、`test_connector_deleted_snapshot.py`；OAuth 供应商响应为受控边界 |
| Dataset CRUD/search | `/datasets`、`/datasets/search`、`/datasets/{id}/search` | `test_dataset_restful_api.py`、`test_dataset_search_route.py`、`test_dataset_auth.py` | `test_dataset_management_http.py`、`test_dataset_search_http.py`、`test_dataset_access_permissions.py` |
| Dataset index/graph | `/datasets/{id}/index`、`/graph` 与已声明 deprecated 入口 | `test_dataset_index_api.py`、`test_dataset_knowledge_graph_route.py`、`test_dataset_raptor_scope.py` | `test_dataset_index_http.py`、`test_dataset_raptor_scope.py`；writer/取消失败专项保留 |
| Dataset tags/metadata/ingestion/embedding | tags、metadata/config、ingestions、embedding/check | `test_dataset_index_api.py`、`test_dataset_metadata_config.py`、`test_dataset_embedding.py` | `test_dataset_management_http.py`、`test_dataset_embedding_http.py`、`test_metadata_config_contract.py` |
| Document raw | `/documents/images/{path}`、`/documents/{id}/preview`、download、artifact | `test_document_image_routes.py`、`test_document_file_http.py`、`test_sandbox_artifact_routes.py` | `test_document_image_http.py`、`test_document_file_http.py`、`test_sandbox_artifact_access.py` |
| Document management | `/datasets/{id}/documents`、parse/stop、PATCH、metadatas | `test_restful_document_*_route.py`、`test_document_parse_routes.py`、`test_document_update_route.py` | `test_document_ingest.py`、`test_document_parser_update.py`、`test_document_delete_scope.py`、`test_document_metadata_filter_http.py` |
| Files/link | `/files`、move、parent/ancestors、link-to-datasets | `test_file_restful_api.py`、`test_file_api_service.py` | `test_file_link_contract.py`、`test_file_ancestry_security.py`、`test_file_move_cycle.py`、`test_file_batch_delete.py` |
| Langfuse | `/langfuse/api-key` | `test_llm4tenant_langfuse.py`、`test_langfuse_host_boundary.py` | 供应商探测受控；没有 live Langfuse 验收 |
| MCP | `/mcp/servers`、import、tools/test | `test_mcp_restful_api.py`、`test_mcp_tool_binding.py`、`test_mcp_delegation*.py` | `test_mcp_interaction_repository.py`、`test_mcp_skill_distribution.py`；真实 MCP 协议专项另见 `make mcp-compat` |
| Memories/messages | `/memories`、config、`/messages`、search/content | `test_memory_restful_api.py`、`test_memory_authorization.py`、`test_memory_message_attribution.py` | `test_memory_authorization.py`、`test_memory_principal_isolation.py` |
| Chat audio | `/chat/audio/transcription`、`/chat/audio/speech` | `test_chat_crud_routes.py` | `test_rest_chat_audio_contract.py`；供应商受控，临时文件及错误路径独立读回 |
| OpenAI chat compatibility | `/openai/{id}/chat/completions`、deprecated `/chats_openai/{id}/chat/completions` | `test_openai_compatible_api.py` | `test_rest_chat_completion_contract.py`；模型输出/传输故障受控，真实供应商与模型质量另验 |
| Plugin tools | `/plugin/tools` | `test_stats_plugin_restful_api.py`、`test_llm_tool_capability_config.py` | 当前表不宣称 live plugin 执行验收 |
| Retrieval | `/datasets/search`、`/datasets/{id}/search`、SDK `/retrieval` | `test_dataset_search_route.py`、`test_sdk_retrieval_metadata_scope.py`、`test_dify_retrieval_contract.py` | `test_dataset_search_http.py`、`test_retrieval_parent_http.py`、`test_retrieval_parent_fallback.py` |
| Search apps | `/searches`、`/searches/{id}/completions` | `test_search_restful_api.py`、`test_async_chat_routes.py` | `test_dialog_sql_scope.py`；模型边界受控 |
| System | `/system/ping`、healthz、version、status、tokens、stats、config/log | `test_system_restful_api.py`、`test_stats_plugin_restful_api.py` | 独立 `make smoke` 检查启动与依赖；无服务时 unit 仅验证响应合同 |
| Task control | `/tasks/{id}/cancel`、PATCH task | `test_task_cancellation.py` | `test_task_cancellation.py`、`test_task_cancellation_terminal.py`、`test_worker_process_recovery.py` |
| User/tenant | auth/login、OAuth、password reset、`/users/me`、models、`/tenants` 与成员 | `test_user_restful_api.py`、`test_user_password_reset.py`、`test_tenant_restful_api.py`、`test_tenant_member_management.py` | `test_user_password_reset.py`、`test_principal_auth.py`；邮件/OAuth 供应商边界受控 |
| Router/authentication | 所有受保护族的无身份拒绝、未知路径 JSON 404、已退役 Agent OpenAI 路径 | `test_restful_route_contracts.py`：真实 app、移除鉴权覆盖、断言零 DB/Agent 执行 | JWT/API key 的归属及写入/拒绝副作用分别由上述真实 SQL 专项验证 |

表中 unit 文件位于 `unit/`，真实服务文件位于 `integration/`；同名文件代表两层不同证据。
新增共享假件放 `support/`，测试不得相互导入，也不通过 `sys.modules` 整包替换应用。
已有 `client` fixture 使用真实 FastAPI app，替换明确的服务依赖；不会启动生产进程。

## Infinity 与 CI

`InfinityConnection.get()` 直接读回必须包含 `id`，同时保留逻辑字段映射、合法零值和
连接归还；REST chunk detail 将该字典作为响应来源。`unit/test_infinity_get.py` 验证
实际索引列表形状、数据集与索引绑定、缺失与故障语义以及真实 chunk HTTP handler。
`integration/test_infinity_available_filter.py` 对真实 Infinity 做 get/update/delete 与独立 SDK 读回。
该文件属于独立 Infinity 分组，未混入 core，也不替代 PostgreSQL/其他文档引擎专项。

CI 自动收集 `unit/`；core 四分片、Infinity、consumer 与 smoke 按
[执行与证据说明](README.md#ci-分层与证据) 分层运行。新增 REST 测试无需创建另一套容器工作流，
也不移除旧测试中的未替代覆盖。测试文件存在和 CI 配置可收集均不等于远程检查已运行。
Web consumer 必须绑定固定提交；无身份 unit 不能代替真实 JWT/API key 权限矩阵或存储读回。
