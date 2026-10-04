"""Protect managed skill trees from generic File mutations."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db.db_models import File

SKILL_SOURCES = ("skill_space", "skill", "skill_version", "skill_file")


def is_skill_managed(db: Session, identity: str, *, descendants: bool = False) -> bool:
    """Inspect ancestry (and optionally descendants), including malformed cycles."""
    tree = select(File.id, File.parent_id, File.source_type).where(File.id == identity).cte("skill_managed_tree", recursive=True)
    tree = tree.union(select(File.id, File.parent_id, File.source_type).join(tree, File.parent_id == tree.c.id if descendants else File.id == tree.c.parent_id))
    return db.scalar(select(tree.c.id).where(tree.c.source_type.in_(SKILL_SOURCES)).limit(1)) is not None
