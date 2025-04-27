"""add columns

Revision ID: b7904d91f00f
Revises: a08220dd999f
Create Date: 2025-04-22 09:17:14.501004

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7904d91f00f'
down_revision: Union[str, None] = 'a08220dd999f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        DO $$
        BEGIN
            -- 检查 priority 列是否存在于 t_ai_tasks 表
            IF NOT EXISTS (
                SELECT 1 
                FROM information_schema.columns 
                WHERE table_schema = 'usr_ai' 
                AND table_name = 't_ai_tasks' 
                AND column_name = 'priority'
            ) THEN
                ALTER TABLE usr_ai.t_ai_tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
            END IF;
        END
        $$;
        """)



def downgrade() -> None:
    """Downgrade schema."""
    pass
