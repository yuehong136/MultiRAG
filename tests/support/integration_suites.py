"""Explicit integration boundaries, resolved before importing application modules.

New files conservatively belong to core and require the full service stack.
Promote a file to DATABASE_TESTS only after verifying its runtime dependencies.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = ROOT / "tests" / "integration"
EVALS = ROOT / "tests" / "evals"
DATABASE_TESTS = frozenset(
    {
        "test_async_engine.py",
        "test_channel_candidate_gc.py",
        "test_channel_control_persistence.py",
        "test_channel_history_manager.py",
        "test_channel_identity_resolution.py",
        "test_channel_interaction_repository.py",
        "test_common_service_crud.py",
        "test_db_bootstrap.py",
        "test_database_process_recovery.py",
        "test_document_existence.py",
        "test_enterprise_subject_repository.py",
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
        "test_pdf_page_task_defaults.py",
        "test_principal_auth.py",
        "test_sync_log_checkpoint.py",
    }
)
INFINITY_TESTS = frozenset({"test_infinity_available_filter.py"})

ISOLATED_STORAGE_TESTS = frozenset(
    {
        "test_agent_completion_close.py",
        "test_agent_execution_origin.py",
        "test_agent_list_operations.py",
        "test_agent_session_references.py",
        "test_agent_update_release.py",
        "test_agentbot_list_operations.py",
        "test_asana_deleted_sync.py",
        "test_chat_session_compat.py",
        "test_connector_deleted_snapshot.py",
        "test_connector_google_oauth_pkce.py",
        "test_dataset_async_management.py",
        "test_dataset_index_http.py",
        "test_dataset_management_http.py",
        "test_dataset_raptor_scope.py",
        "test_dataset_search_http.py",
        "test_debug_response_start.py",
        "test_document_delete_scope.py",
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
        "test_gmail_deleted_sync.py",
        "test_integration_resource_cleanup.py",
        "test_memory_principal_isolation.py",
        "test_metadata_config_contract.py",
        "test_paddleocr_service_contract.py",
        "test_pdf_outline_metadata.py",
        "test_pipeline_parser_cleanup.py",
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
PARALLEL_TESTS = DATABASE_TESTS | ISOLATED_STORAGE_TESTS

SUITES = ("core", "db", "system", "infinity", "consumer", "all", "eval", "eval-generation")


def suite_paths(suite: str) -> list[Path]:
    if suite not in SUITES:
        raise ValueError(f"Unknown integration suite: {suite}")
    files = sorted(INTEGRATION.glob("test_*.py"))
    if suite == "eval":
        return [EVALS / "test_quality.py"]
    if suite == "eval-generation":
        return [EVALS / "test_generation.py"]
    if suite == "db":
        return [path for path in files if path.name in DATABASE_TESTS]
    if suite == "system":
        return [INTEGRATION / name for name in ("test_database_process_recovery.py", "test_worker_process_recovery.py")]
    if suite == "infinity":
        return [path for path in files if path.name in INFINITY_TESTS]
    if suite == "consumer":
        return [INTEGRATION / name for name in ("test_dataset_search_http.py", "test_dataset_management_http.py")]
    if suite == "core":
        return [path for path in files if path.name not in INFINITY_TESTS]
    return files


def required_services(paths: list[Path]) -> set[str]:
    required: set[str] = set()
    for path in paths:
        if path.name in INFINITY_TESTS:
            required.add("infinity")
        elif path.name in DATABASE_TESTS:
            required.add("postgresql")
        else:
            required.update(("postgresql", "redis", "minio", "milvus"))
    return required
