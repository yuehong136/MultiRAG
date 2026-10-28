"""Explicit integration boundaries, resolved before importing application modules.

New files conservatively belong to core and require the full service stack.
Promote a file to DATABASE_TESTS only after verifying its runtime dependencies.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = ROOT / "tests" / "integration"
EVALS = ROOT / "tests" / "evals"
ACCEPTANCE = ROOT / "tests" / "acceptance"
DATABASE_TESTS = frozenset(
    {
        "test_agent_retrieval_top_k.py",
        "test_async_engine.py",
        "test_channel_candidate_gc.py",
        "test_channel_control_persistence.py",
        "test_channel_history_manager.py",
        "test_channel_identity_resolution.py",
        "test_channel_interaction_repository.py",
        "test_common_service_crud.py",
        "test_db_bootstrap.py",
        "test_database_process_recovery.py",
        "test_dialog_sql_scope.py",
        "test_dataset_access_permissions.py",
        "test_rest_list_pagination_order.py",
        "test_dataset_raptor_scope.py",
        "test_document_existence.py",
        "test_enterprise_subject_repository.py",
        "test_evaluation_token_usage.py",
        "test_exesql_recovery.py",
        "test_identity_channel_credentials.py",
        "test_identity_directory_events.py",
        "test_identity_onboarding.py",
        "test_identity_provisioning.py",
        "test_identity_provisioning_schema.py",
        "test_identity_reconciliation_recovery.py",
        "test_identity_reconciliation_repository.py",
        "test_identity_repository.py",
        "test_identity_schema.py",
        "test_mcp_interaction_repository.py",
        "test_mcp_interaction_schema.py",
        "test_llm_embedding_inputs.py",
        "test_llm_tool_capability.py",
        "test_model_config_resolution.py",
        "test_provider_instances.py",
        "test_pdf_page_task_defaults.py",
        "test_principal_auth.py",
        "test_retrieval_parent_fallback.py",
        "test_retrieval_rerank_authorization.py",
        "test_sandbox_provider_settings.py",
        "test_sync_log_checkpoint.py",
        "test_task_log_limits.py",
    }
)
REDIS_TESTS = frozenset({"test_shared_auth_secret.py", "test_synonym_redis.py"})
INFINITY_TESTS = frozenset(
    {"test_infinity_available_filter.py", "test_infinity_metadata_contention.py", "test_infinity_stable_release.py", "test_infinity_raptor_extra.py", "test_metadata_infinity_pushdown.py"}
)

ISOLATED_STORAGE_TESTS = frozenset(
    {
        "test_agent_tags.py",
        "test_agent_completion_close.py",
        "test_agent_execution_origin.py",
        "test_agent_literal_bool_semantics.py",
        "test_agent_list_operations.py",
        "test_agent_session_references.py",
        "test_agent_update_release.py",
        "test_agentbot_list_operations.py",
        "test_agentbot_structured_output.py",
        "test_asana_deleted_sync.py",
        "test_azure_storage_protocol.py",
        "test_blob_connector_fingerprint.py",
        "test_chat_session_compat.py",
        "test_connector_deleted_snapshot.py",
        "test_connector_google_oauth_pkce.py",
        "test_dataset_async_management.py",
        "test_dataset_index_http.py",
        "test_dataset_management_http.py",
        "test_dataset_search_http.py",
        "test_debug_response_start.py",
        "test_document_delete_scope.py",
        "test_document_file_http.py",
        "test_document_image_http.py",
        "test_document_image_read_service.py",
        "test_document_ingest.py",
        "test_document_parse_retirement.py",
        "test_document_parser_update.py",
        "test_document_source_recovery.py",
        "test_document_status.py",
        "test_dropbox_deleted_sync.py",
        "test_file_batch_delete.py",
        "test_gitlab_deleted_sync.py",
        "test_github_sync_defaults.py",
        "test_gmail_deleted_sync.py",
        "test_integration_resource_cleanup.py",
        "test_memory_principal_isolation.py",
        "test_metadata_config_contract.py",
        "test_paddleocr_service_contract.py",
        "test_pdf_outline_metadata.py",
        "test_pipeline_parser_cleanup.py",
        "test_rest_api_sync.py",
        "test_runtime_chat_attachments.py",
        "test_runtime_document_upload.py",
        "test_runtime_upload_browser_guard.py",
        "test_sandbox_artifact_access.py",
        "test_seafile_deleted_sync.py",
        "test_services_connectivity.py",
        "test_skill_assets.py",
        "test_skill_http.py",
        "test_skill_search_store.py",
        "test_task_cancellation.py",
        "test_task_cancellation_terminal.py",
        "test_task_metadata_generation.py",
        "test_worker_process_recovery.py",
        "test_zendesk_deleted_sync.py",
    }
)
# These exercise process-wide runtime seams and worker imports. Keep the full
# application service stack and serial execution even if another group grows.
SERIAL_STORAGE_TESTS = frozenset(
    {"test_agent_file_authorization.py", "test_legacy_document_counters.py", "test_outlook_api.py", "test_outlook_worker.py", "test_after53_dataset_create_web.py", "test_chunk_image_creation.py"}
)
PARALLEL_TESTS = (DATABASE_TESTS | ISOLATED_STORAGE_TESTS) - SERIAL_STORAGE_TESTS

SUITES = ("core", "db", "system", "infinity", "consumer", "all", "eval", "eval-generation", "acceptance")


def suite_paths(suite: str) -> list[Path]:
    if suite not in SUITES:
        raise ValueError(f"Unknown integration suite: {suite}")
    files = sorted(INTEGRATION.glob("test_*.py"))
    if suite == "eval":
        return [EVALS / "test_quality.py"]
    if suite == "eval-generation":
        return [EVALS / "test_generation.py"]
    if suite == "acceptance":
        return [ACCEPTANCE / "test_product.py"]
    if suite == "db":
        return [path for path in files if path.name in DATABASE_TESTS]
    if suite == "system":
        return [INTEGRATION / name for name in ("test_database_process_recovery.py", "test_worker_process_recovery.py")]
    if suite == "infinity":
        return [path for path in files if path.name in INFINITY_TESTS]
    if suite == "consumer":
        return [
            INTEGRATION / name
            for name in (
                "test_after4776_web_contracts.py",
                "test_after53_dataset_create_web.py",
                "test_agent_draft_session_persistence.py",
                "test_browser_web_consumer.py",
                "test_app_list_web_consumers.py",
                "test_dataset_search_http.py",
                "test_dataset_management_http.py",
                "test_dataset_embedding_http.py",
                "test_connector_web_sync.py",
                "test_connector_dashboard_consumer.py",
                "test_chunk_image_creation.py",
                "test_connector_rebuild_authorization.py",
                "test_github_sync_defaults.py",
                "test_model_document_web_consumers.py",
                "test_outlook_api.py",
                "test_provider_web_consumers.py",
                "test_retrieval_rerank_authorization.py",
                "test_document_file_http.py",
                "test_session_upload_cleanup.py",
                "test_metadata_web_consumers.py",
                "test_metadata_config_contract.py",
                "test_shared_chunk_owner_contract.py",
                "test_sandbox_provider_settings.py",
            )
        ]
    if suite == "core":
        return [path for path in files if path.name not in INFINITY_TESTS]
    return files


def required_services(paths: list[Path]) -> set[str]:
    required: set[str] = set()
    for path in paths:
        if path.name in {"test_agent_lifecycle_source_contracts.py", "test_after4776_web_contracts.py"}:
            required.update(("postgresql", "redis", "minio"))
            continue
        if path.name == "test_connector_task_controls.py":
            required.add("postgresql")
            continue
        if path.name == "test_tts_cache.py":
            required.update(("postgresql", "redis"))
            continue
        if path.name == "test_retrieval_manual_filter_iteration.py":
            required.update(("postgresql", "elasticsearch"))
            continue
        if path.name == "test_runtime_cleanup_storage_protocol.py":
            # Real cloud SDK clients use owned transports and OpenDAL memory.
            continue
        if path.name == "test_metadata_web_consumers.py":
            required.update(("postgresql", "redis", "minio", "milvus", "infinity", "elasticsearch"))
            continue
        if path.name == "test_metadata_infinity_pushdown.py":
            required.update(("postgresql", "infinity"))
            continue
        if path.name == "test_es_server_scoring.py":
            required.update(("postgresql", "elasticsearch", "opensearch"))
            continue
        if path.name == "test_raptor_search_storage.py":
            required.add("elasticsearch")
            continue
        if path.name == "test_raptor_psi_storage.py":
            required.update(("postgresql", "milvus"))
            continue
        if path.name == "test_document_ingest.py":
            required.update(("elasticsearch", "opensearch"))
        if path.name == "test_es_os_doc_visibility.py":
            required.update(("elasticsearch", "opensearch"))
            continue
        if path.name == "test_es_conn_keyword_ids.py":
            required.add("elasticsearch")
            continue
        if path.name == "test_azure_storage_protocol.py":
            # Real SDKs with an owned HTTP transport; no infrastructure needed.
            continue
        if path.name == "test_metadata_lazy_pushdown.py":
            required.update(("postgresql", "elasticsearch"))
            continue
        if path.name in SERIAL_STORAGE_TESTS:
            required.update(("postgresql", "redis", "minio", "milvus"))
        elif path.name in INFINITY_TESTS:
            required.add("infinity")
        elif path.name in REDIS_TESTS:
            required.add("redis")
        elif path.name in DATABASE_TESTS:
            required.add("postgresql")
        else:
            required.update(("postgresql", "redis", "minio", "milvus"))
    return required
