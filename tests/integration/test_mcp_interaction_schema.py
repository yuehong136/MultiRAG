"""PostgreSQL schema contracts for EIM-U14 durable interactions."""

from __future__ import annotations

import sqlalchemy as sa

from api.db.db_models import McpInteraction, McpInteractionResumeJob

_SCHEMA = "usr_ai"
_U15_REVISION = "d8f0a2b4c6e8"


def test_u15_is_the_single_alembic_head(alembic_cfg) -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_cfg)

    assert script.get_heads() == [_U15_REVISION]


def test_fresh_schema_has_interaction_and_resume_job_constraints(
    bootstrapped_engine: sa.Engine,
) -> None:
    inspector = sa.inspect(bootstrapped_engine)

    assert inspector.has_table(McpInteraction.__tablename__, schema=_SCHEMA)
    assert inspector.has_table(McpInteractionResumeJob.__tablename__, schema=_SCHEMA)
    interaction_columns = {column["name"] for column in inspector.get_columns(McpInteraction.__tablename__, schema=_SCHEMA)}
    job_columns = {
        column["name"]
        for column in inspector.get_columns(
            McpInteractionResumeJob.__tablename__,
            schema=_SCHEMA,
        )
    }

    assert {
        "id",
        "tenant_id",
        "platform_user_id",
        "external_identity_id",
        "identity_revision",
        "agent_id",
        "agent_revision_id",
        "mcp_server_id",
        "resource_name",
        "resource_uri",
        "tool_name",
        "call_digest",
        "schema_digest",
        "output_schema_digest",
        "output_schema_ciphertext",
        "output_schema_key_id",
        "original_arguments_ciphertext",
        "original_arguments_key_id",
        "input_requests_ciphertext",
        "input_requests_key_id",
        "request_state_ciphertext",
        "request_state_key_id",
        "policy_revision",
        "credential_generation",
        "effect",
        "replay_mode",
        "state",
        "revision",
        "round_count",
        "expires_at",
        "result_ciphertext",
        "result_key_id",
        "provider",
        "presentation_ref",
        "created_at",
        "updated_at",
    }.issubset(interaction_columns)
    assert {
        "id",
        "interaction_id",
        "tenant_id",
        "revision",
        "response_idempotency_key",
        "input_response_ciphertext",
        "input_response_key_id",
        "state",
        "lease_owner",
        "lease_until",
        "attempt",
        "next_attempt_at",
        "safe_error_code",
        "created_at",
        "updated_at",
    }.issubset(job_columns)

    interaction_checks = {
        item["name"]
        for item in inspector.get_check_constraints(
            McpInteraction.__tablename__,
            schema=_SCHEMA,
        )
    }
    job_checks = {
        item["name"]
        for item in inspector.get_check_constraints(
            McpInteractionResumeJob.__tablename__,
            schema=_SCHEMA,
        )
    }
    assert {
        "ck_mcp_interactions_state",
        "ck_mcp_interactions_effect",
        "ck_mcp_interactions_replay_mode",
        "ck_mcp_interactions_revision_round",
        "ck_mcp_interactions_digest",
        "ck_mcp_interactions_output_schema_payload",
    }.issubset(interaction_checks)
    assert {
        "ck_mcp_interaction_jobs_state",
        "ck_mcp_interaction_jobs_revision_attempt",
        "ck_mcp_interaction_jobs_lease",
    }.issubset(job_checks)
