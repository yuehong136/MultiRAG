"""add colums

Revision ID: a08220dd999f
Revises: 60e7c845e76c
Create Date: 2025-04-17 10:27:47.094225

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a08220dd999f'
down_revision: Union[str, None] = '60e7c845e76c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        DO $$
        BEGIN
            -- 检查 task_type 列是否存在于 t_ai_tasks 表
            IF NOT EXISTS (
                SELECT 1 
                FROM information_schema.columns 
                WHERE table_schema = 'usr_ai' 
                AND table_name = 't_ai_tasks' 
                AND column_name = 'task_type'
            ) THEN
                ALTER TABLE usr_ai.t_ai_tasks ADD COLUMN task_type VARCHAR(32) NOT NULL DEFAULT '';
            END IF;
        END
        $$;
        """)


def downgrade() -> None:
    """Downgrade schema."""
    pass
