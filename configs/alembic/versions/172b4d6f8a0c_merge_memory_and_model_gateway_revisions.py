"""Merge memory and model gateway revisions

Revision ID: 172b4d6f8a0c
Revises: 0d35f7a9c1e3, f3b5d7e9a1c2
Create Date: 2026-11-22 16:41:44.012828

"""

revision: str = "172b4d6f8a0c"
down_revision: tuple[str, str] = ("0d35f7a9c1e3", "f3b5d7e9a1c2")
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """Join the two existing schema histories."""


def downgrade() -> None:
    """Restore the independent revision heads."""
