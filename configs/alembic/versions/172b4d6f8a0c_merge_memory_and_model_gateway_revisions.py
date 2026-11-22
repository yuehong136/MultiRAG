"""Merge memory and model gateway revisions

Revision ID: 172b4d6f8a0c
Revises: 0d35f7a9c1e3, f3b5d7e9a1c2
Create Date: 2026-11-22 16:41:44.012828

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "172b4d6f8a0c"
down_revision: Union[str, None] = ("0d35f7a9c1e3", "f3b5d7e9a1c2")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
