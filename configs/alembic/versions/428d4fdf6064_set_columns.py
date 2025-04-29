"""set columns

Revision ID: 428d4fdf6064
Revises: b7904d91f00f
Create Date: 2025-04-29 14:28:18.742679

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '428d4fdf6064'
down_revision: Union[str, None] = 'b7904d91f00f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 修改 t_ai_tenant_llms.api_key 长度 1024→2048
    # batch_alter_table 会在 SQLite 下重建表，在 MySQL、PostgreSQL 下也能正确执行
    with op.batch_alter_table("t_ai_tenant_llms", schema="usr_ai") as batch_op:
        batch_op.alter_column(
            "api_key",
            existing_type=sa.String(length=1024),
            type_=sa.String(length=2048),
            existing_nullable=True,
        )



def downgrade() -> None:
    # 如果要回滚，将 api_key 改回 1024
    with op.batch_alter_table("t_ai_tenant_llms", schema="usr_ai") as batch_op:
        batch_op.alter_column(
            "api_key",
            existing_type=sa.String(length=2048),
            type_=sa.String(length=1024),
            existing_nullable=True,
        )
