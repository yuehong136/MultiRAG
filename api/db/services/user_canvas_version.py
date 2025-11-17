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
    def get_all_canvas_version_by_canvas_ids(cls, db: Session, canvas_ids: list[str]):
        """根据canvas_ids批量查询所有版本ID，使用分页避免内存溢出"""
        stmt = (
            select(cls.model.id)
            .where(cls.model.user_canvas_id.in_(canvas_ids))
            .order_by(cls.model.create_time.asc())
        )

        offset, limit = 0, 100
        res = []

        while True:
            try:
                version_batch = db.execute(
                    stmt.offset(offset).limit(limit)
                ).scalars().all()

                if not version_batch:
                    break

                res.extend([{"id": version_id} for version_id in version_batch])
                offset += limit
            except Exception:
                logging.exception("Failed to get canvas versions for batch at offset %d", offset)
                break

        return res

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


