"""Protect managed skill trees from generic File mutations."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db.db_models import File, PythonSkillCoreSpace

SKILL_SOURCES = ("skill_space", "skill", "skill_version", "skill_file", "skill_space_core")


def is_skill_managed(db: Session, identity: str, *, descendants: bool = False) -> bool:
    """Inspect ancestry (and optionally descendants), including malformed cycles."""
    tree = select(File.id, File.parent_id, File.source_type).where(File.id == identity).cte("skill_managed_tree", recursive=True)
    tree = tree.union(select(File.id, File.parent_id, File.source_type).join(tree, File.parent_id == tree.c.id if descendants else File.id == tree.c.parent_id))
    if db.scalar(select(tree.c.id).where(tree.c.source_type.in_(SKILL_SOURCES)).limit(1)) is not None:
        return True
    spaces = db.scalars(select(PythonSkillCoreSpace).where(PythonSkillCoreSpace.folder_id.in_(select(tree.c.id)))).all()
    for space in spaces:
        if space.state != "active":
            return True
        if any(identity in plan["ids"] for plan in space.cleanup.get("files", [])) or identity in space.cleanup.get("uploads", {}).get("ids", []):
            return True
    return False


def is_python_core(db: Session, identity: str, *, descendants: bool = False) -> bool:
    tree = select(File.id, File.parent_id, File.source_type).where(File.id == identity).cte("python_core_tree", recursive=True)
    tree = tree.union(select(File.id, File.parent_id, File.source_type).join(tree, File.parent_id == tree.c.id if descendants else File.id == tree.c.parent_id))
    return db.scalar(select(tree.c.id).where(tree.c.source_type == "python_skill_space_core").limit(1)) is not None
