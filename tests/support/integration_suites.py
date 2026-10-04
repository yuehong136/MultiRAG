"""Explicit integration boundaries, resolved before importing application modules.

New files conservatively belong to core and require the full service stack.
Promote a file to DATABASE_TESTS only after verifying its runtime dependencies.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = ROOT / "tests" / "integration"
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
SUITES = ("core", "db", "system", "infinity", "consumer", "all")


def suite_paths(suite: str) -> list[Path]:
    if suite not in SUITES:
        raise ValueError(f"Unknown integration suite: {suite}")
    files = sorted(INTEGRATION.glob("test_*.py"))
    if suite == "db":
        return [path for path in files if path.name in DATABASE_TESTS]
    if suite == "system":
        return [INTEGRATION / "test_database_process_recovery.py"]
    if suite == "infinity":
        return [path for path in files if path.name in INFINITY_TESTS]
    if suite == "consumer":
        return [INTEGRATION / "test_dataset_search_http.py"]
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
