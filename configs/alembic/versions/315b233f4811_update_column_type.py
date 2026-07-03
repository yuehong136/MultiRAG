"""update column type

Revision ID: 315b233f4811
Revises: 24df210f5ebf
Create Date: 2025-11-03 14:32:41.180953

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision: str = '315b233f4811'
down_revision: str | None = '24df210f5ebf'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    dialect_name = bind.dialect.name

    # 检查表是否存在
    tables = insp.get_table_names(schema="usr_ai")

    # ===== 更新 t_ai_tenant_llms 表的 api_key 字段类型 =====
    # 从 String(2048) 更新为 Text，并移除索引
    if "t_ai_tenant_llms" in tables:
        columns = {col["name"]: col for col in insp.get_columns("t_ai_tenant_llms", schema="usr_ai")}

        if "api_key" in columns:
            # 检查索引是否存在
            indexes = insp.get_indexes("t_ai_tenant_llms", schema="usr_ai")
            index_names = [idx["name"] for idx in indexes]

            with op.batch_alter_table("t_ai_tenant_llms", schema="usr_ai") as batch_op:
                # 先移除索引（如果存在）
                if "ix_usr_ai_t_ai_tenant_llms_api_key" in index_names:
                    batch_op.drop_index("ix_usr_ai_t_ai_tenant_llms_api_key")

                # 修改列类型
                batch_op.alter_column(
                    "api_key",
                    existing_type=sa.String(length=2048),
                    type_=sa.Text(),
                    existing_nullable=True,
                    nullable=True,
                    comment="API KEY"
                )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 检查表是否存在
    tables = insp.get_table_names(schema="usr_ai")

    # ===== 回滚 t_ai_tenant_llms 表的 api_key 字段类型 =====
    # 从 Text 回滚为 String(2048)，并重新添加索引
    if "t_ai_tenant_llms" in tables:
        columns = {col["name"]: col for col in insp.get_columns("t_ai_tenant_llms", schema="usr_ai")}

        if "api_key" in columns:
            # 检查索引是否存在
            indexes = insp.get_indexes("t_ai_tenant_llms", schema="usr_ai")
            index_names = [idx["name"] for idx in indexes]

            with op.batch_alter_table("t_ai_tenant_llms", schema="usr_ai") as batch_op:
                # 修改列类型回 String(2048)
                batch_op.alter_column(
                    "api_key",
                    existing_type=sa.Text(),
                    type_=sa.String(length=2048),
                    existing_nullable=True,
                    nullable=True
                )

                # 重新添加索引（如果不存在）
                if "ix_usr_ai_t_ai_tenant_llms_api_key" not in index_names:
                    batch_op.create_index(
                        "ix_usr_ai_t_ai_tenant_llms_api_key",
                        ["api_key"]
                    )
