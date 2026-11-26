"""Record what gateway calls cost and keep usage per day.

Request logs gain the call's cost and its cached prompt tokens, model settings
an optional price, and ``t_ai_gateway_daily_usage`` sums every call per UTC
day, key, model and endpoint. Existing logs are folded into the daily table;
their cost stays unknown, because the price at the time was not recorded.
An index on ``started_at`` serves log retention.

Each step is skipped when a table created from the current models already
has it, so both bootstrap paths end in the same schema.

Revision ID: 0a2c4e6f8b1d
Revises: f7a9c2e4b6d8
"""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0a2c4e6f8b1d"
down_revision: str | None = "f7a9c2e4b6d8"
branch_labels: str | None = None
depends_on: str | None = None
SCHEMA = "usr_ai"
_LOGS = "t_ai_gateway_request_logs"
_SETTINGS = "t_ai_gateway_model_settings"
_DAILY = "t_ai_gateway_daily_usage"
_STARTED_INDEX = "ix_gateway_request_logs_started"


def _added_columns() -> dict[str, list[sa.Column[Any]]]:
    return {
        _LOGS: [sa.Column("cache_read_input_tokens", sa.BigInteger(), nullable=True), sa.Column("cost", sa.Numeric(20, 10), nullable=True)],
        _SETTINGS: [
            sa.Column("input_cost_per_token", sa.Numeric(20, 12), nullable=True),
            sa.Column("output_cost_per_token", sa.Numeric(20, 12), nullable=True),
            sa.Column("cache_read_input_token_cost", sa.Numeric(20, 12), nullable=True),
        ],
    }


def _counter(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.BigInteger(), nullable=False, server_default="0")


def _daily() -> sa.Table:
    return sa.Table(
        _DAILY,
        sa.MetaData(schema=SCHEMA),
        sa.Column("create_date", sa.DateTime(), nullable=True),
        sa.Column("update_date", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("key_id", sa.String(32), nullable=False),
        sa.Column("key_name", sa.String(128), nullable=False),
        sa.Column("model_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("model_name", sa.String(255), nullable=True),
        sa.Column("endpoint", sa.String(32), nullable=False),
        _counter("prompt_tokens"),
        _counter("completion_tokens"),
        _counter("cache_read_input_tokens"),
        _counter("total_tokens"),
        sa.Column("spend", sa.Numeric(20, 10), nullable=False, server_default="0"),
        _counter("api_requests"),
        _counter("successful_requests"),
        _counter("failed_requests"),
        _counter("usage_unknown_requests"),
        _counter("total_response_time_ms"),
        sa.UniqueConstraint("tenant_id", "date", "key_id", "model_id", "endpoint", name="uq_gateway_daily_usage_bucket"),
        sa.Index("ix_gateway_daily_usage_key_date", "key_id", "date"),
    )


def _backfill(bind: sa.Connection) -> None:
    """Fold the existing request logs into the daily table, one row per bucket."""
    logs, daily = f'"{SCHEMA}".{_LOGS}', f'"{SCHEMA}".{_DAILY}'
    bind.execute(
        sa.text(
            f"""
            INSERT INTO {daily} (
                id, tenant_id, date, key_id, key_name, model_id, model_name, endpoint,
                prompt_tokens, completion_tokens, cache_read_input_tokens, total_tokens, spend,
                api_requests, successful_requests, failed_requests, usage_unknown_requests, total_response_time_ms
            )
            SELECT
                md5(tenant_id || '/' || day::text || '/' || key_id || '/' || model_id || '/' || endpoint),
                tenant_id, day, key_id, key_name, model_id, model_name, endpoint,
                prompt_tokens, completion_tokens, cache_read_input_tokens, total_tokens, spend,
                api_requests, api_requests - failed_requests, failed_requests, usage_unknown_requests, total_response_time_ms
            FROM (
                SELECT
                    tenant_id,
                    (started_at AT TIME ZONE 'UTC')::date AS day,
                    key_id,
                    COALESCE(upstream_model_id, '') AS model_id,
                    endpoint,
                    max(key_name) AS key_name,
                    max(upstream_model_name) AS model_name,
                    COALESCE(sum(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(sum(completion_tokens), 0) AS completion_tokens,
                    COALESCE(sum(cache_read_input_tokens), 0) AS cache_read_input_tokens,
                    COALESCE(sum(total_tokens), 0) AS total_tokens,
                    COALESCE(sum(cost), 0) AS spend,
                    count(*) AS api_requests,
                    count(*) FILTER (WHERE error_type IS NOT NULL OR status_code >= 400) AS failed_requests,
                    count(*) FILTER (WHERE usage_source = 'unavailable') AS usage_unknown_requests,
                    COALESCE(sum(duration_ms), 0) AS total_response_time_ms
                FROM {logs}
                GROUP BY 1, 2, 3, 4, 5
            ) AS buckets
            """
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    for table, columns in _added_columns().items():
        present = {column["name"] for column in sa.inspect(bind).get_columns(table, schema=SCHEMA)}
        for column in columns:
            if column.name not in present:
                op.add_column(table, column, schema=SCHEMA)
    if _STARTED_INDEX not in {index["name"] for index in sa.inspect(bind).get_indexes(_LOGS, schema=SCHEMA)}:
        op.create_index(_STARTED_INDEX, _LOGS, ["started_at"], schema=SCHEMA)
    if not sa.inspect(bind).has_table(_DAILY, schema=SCHEMA):
        _daily().create(bind)
    if not bind.scalar(sa.select(sa.exists().select_from(_daily()))):
        _backfill(bind)


def downgrade() -> None:
    """Drop the daily sums, the costs and the prices; the request logs keep every call."""
    bind = op.get_bind()
    _daily().drop(bind)
    op.drop_index(_STARTED_INDEX, table_name=_LOGS, schema=SCHEMA)
    for table, columns in _added_columns().items():
        for column in columns:
            op.drop_column(table, column.name, schema=SCHEMA)
