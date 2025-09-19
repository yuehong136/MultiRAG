import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db.db_models import UserCanvasVersion
from api.db.services.common_service import CommonService


class UserCanvasVersionService(CommonService):
    model = UserCanvasVersion

    @classmethod
    def list_by_canvas_id(cls, db: Session, user_canvas_id: str):
        """Return all versions for the specified canvas ordered by newest first."""
        stmt = (
            select(cls.model)
            .where(cls.model.user_canvas_id == user_canvas_id)
            .order_by(cls.model.create_time.desc())
        )
        try:
            return db.execute(stmt).scalars().all()
        except Exception:
            logging.exception("Failed to list canvas versions for %s", user_canvas_id)
            return []

    @classmethod
    def delete_all_versions(cls, db: Session, user_canvas_id: str) -> bool:
        """Keep only the latest 20 versions for the canvas and remove the rest."""
        stmt = (
            select(cls.model.id)
            .where(cls.model.user_canvas_id == user_canvas_id)
            .order_by(cls.model.create_time.desc())
        )
        try:
            version_ids = db.execute(stmt).scalars().all()
            if len(version_ids) > 20:
                cls.delete_by_ids(db, version_ids[20:])
            return True
        except Exception:
            logging.exception("Failed to trim canvas versions for %s", user_canvas_id)
            return False


