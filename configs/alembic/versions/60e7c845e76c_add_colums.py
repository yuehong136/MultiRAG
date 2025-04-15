"""add colums

Revision ID: 60e7c845e76c
Revises: 
Create Date: 2025-04-15 09:49:04.105880

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '60e7c845e76c'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        DO $$
        BEGIN
            -- 检查pagerank列是否存在于t_ai_knowledgebases表
            IF NOT EXISTS (
                SELECT 1 
                FROM information_schema.columns 
                WHERE table_schema = 'usr_ai' 
                AND table_name = 't_ai_knowledgebases' 
                AND column_name = 'pagerank'
            ) THEN
                ALTER TABLE usr_ai.t_ai_knowledgebases ADD COLUMN pagerank INTEGER NOT NULL DEFAULT 0;
            END IF;

            -- 检查meta_fields列是否存在于t_ai_documents表
            IF NOT EXISTS (
                SELECT 1 
                FROM information_schema.columns 
                WHERE table_schema = 'usr_ai' 
                AND table_name = 't_ai_documents' 
                AND column_name = 'meta_fields'
            ) THEN
                ALTER TABLE usr_ai.t_ai_documents ADD COLUMN meta_fields JSONB NOT NULL DEFAULT '{}';
            END IF;
        END
        $$;
        """)


def downgrade() -> None:
    """Downgrade schema."""
    pass
