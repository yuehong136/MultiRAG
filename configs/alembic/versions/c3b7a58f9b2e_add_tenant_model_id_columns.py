"""add tenant model id columns

Revision ID: c3b7a58f9b2e
Revises: 19819cd0b63a
Create Date: 2026-03-24 16:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision: str = "c3b7a58f9b2e"
down_revision: str | None = "19819cd0b63a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    columns = {column["name"] for column in inspector.get_columns(table_name, schema=SCHEMA)}
    return column_name in columns


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _has_column(table_name, column.name):
        op.add_column(table_name, column, schema=SCHEMA)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        op.drop_column(table_name, column_name, schema=SCHEMA)


def upgrade() -> None:
    bigint = sa.BigInteger()

    for column_name in [
        "tenant_llm_id",
        "tenant_embd_id",
        "tenant_asr_id",
        "tenant_img2txt_id",
        "tenant_rerank_id",
        "tenant_tts_id",
    ]:
        _add_column_if_missing("t_ai_tenants", sa.Column(column_name, bigint, nullable=True))

    _add_column_if_missing("t_ai_knowledgebases", sa.Column("tenant_embd_id", bigint, nullable=True))
    _add_column_if_missing("t_ai_dialogs", sa.Column("tenant_llm_id", bigint, nullable=True))
    _add_column_if_missing("t_ai_dialogs", sa.Column("tenant_rerank_id", bigint, nullable=True))
    _add_column_if_missing("t_ai_memories", sa.Column("tenant_embd_id", bigint, nullable=True))
    _add_column_if_missing("t_ai_memories", sa.Column("tenant_llm_id", bigint, nullable=True))


def downgrade() -> None:
    _drop_column_if_exists("t_ai_memories", "tenant_llm_id")
    _drop_column_if_exists("t_ai_memories", "tenant_embd_id")
    _drop_column_if_exists("t_ai_dialogs", "tenant_rerank_id")
    _drop_column_if_exists("t_ai_dialogs", "tenant_llm_id")
    _drop_column_if_exists("t_ai_knowledgebases", "tenant_embd_id")

    for column_name in [
        "tenant_tts_id",
        "tenant_rerank_id",
        "tenant_img2txt_id",
        "tenant_asr_id",
        "tenant_embd_id",
        "tenant_llm_id",
    ]:
        _drop_column_if_exists("t_ai_tenants", column_name)
