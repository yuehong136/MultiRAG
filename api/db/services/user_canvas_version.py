import json
import logging
import time

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from api.db.db_models import UserCanvasVersion
from api.db.services.common_service import CommonService


class UserCanvasVersionService(CommonService):
    model = UserCanvasVersion

    @staticmethod
    def build_version_title(user_nickname, agent_title, ts=None):
        tenant = str(user_nickname or "").strip() or "tenant"
        title = str(agent_title or "").strip() or "agent"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts is not None else time.strftime("%Y-%m-%d %H:%M:%S")
        return "{0}_{1}_{2}".format(tenant, title, stamp)

    @staticmethod
    def _normalize_dsl(dsl):
        normalized = dsl
        if isinstance(normalized, str):
            try:
                normalized = json.loads(normalized)
            except Exception as e:
                raise ValueError("Invalid DSL JSON string.") from e

        if not isinstance(normalized, dict):
            raise ValueError("DSL must be a JSON object.")

        try:
            return json.loads(json.dumps(normalized, ensure_ascii=False))
        except Exception as e:
            raise ValueError("DSL is not JSON-serializable.") from e

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
        """Keep only the latest 20 unpublished versions and remove the rest. Released versions are always kept."""
        stmt = (
            select(cls.model.id)
            .where(
                cls.model.user_canvas_id == user_canvas_id,
                or_(cls.model.release == False, cls.model.release.is_(None)),  # noqa: E712
            )
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

    @classmethod
    def _get_latest_by_canvas_id(cls, db: Session, user_canvas_id: str, only_released: bool = False):
        """Return the newest version for the canvas, optionally filtered by release status."""
        stmt = select(cls.model).where(cls.model.user_canvas_id == user_canvas_id)
        if only_released:
            stmt = stmt.where(cls.model.release.is_(True))
        stmt = stmt.order_by(cls.model.create_time.desc())
        try:
            return db.execute(stmt).scalars().first()
        except Exception:
            logging.exception("Failed to get latest version for %s", user_canvas_id)
            return None

    @classmethod
    def get_latest_released(cls, db: Session, user_canvas_id: str):
        """Return the newest released version for the specified canvas."""
        return cls._get_latest_by_canvas_id(db, user_canvas_id, only_released=True)

    @classmethod
    def get_latest_version_title(cls, db: Session, user_canvas_id: str, release_mode: bool = False) -> str | None:
        """Return the version title for a canvas based on release_mode.

        Args:
            db: Active database session.
            user_canvas_id: The canvas ID.
            release_mode: If True, use the latest released version's title;
                if False, use the latest version's title regardless of release status.
        """
        latest = cls._get_latest_by_canvas_id(db, user_canvas_id, only_released=release_mode)
        return latest.title if latest else None

    @classmethod
    def save_or_replace_latest(cls, db: Session, user_canvas_id: str, dsl, title: str | None = None, description: str | None = None, release=None):
        """
        Persist a canvas snapshot into version history.

        If the latest version has the same DSL content, update that version in place
        instead of creating a new row.

        Exception: If the latest version is released (release=True) and current save is not,
        create a new version to protect the released version.
        """
        try:
            normalized_dsl = cls._normalize_dsl(dsl)
            stmt = (
                select(cls.model)
                .where(cls.model.user_canvas_id == user_canvas_id)
                .order_by(cls.model.create_time.desc())
            )
            latest = db.execute(stmt).scalars().first()

            if latest and cls._normalize_dsl(latest.dsl) == normalized_dsl:
                # Protect released version: if latest is released and current is not,
                # create a new version instead of updating
                if latest.release and not release:
                    insert_data = {"user_canvas_id": user_canvas_id, "dsl": normalized_dsl}
                    if title is not None:
                        insert_data["title"] = title
                    if description is not None:
                        insert_data["description"] = description
                    if release is not None:
                        insert_data["release"] = release
                    cls.insert(db, **insert_data)
                    cls.delete_all_versions(db, user_canvas_id)
                    return None, True

                # Normal case: update existing version
                # DSL unchanged: do NOT update title to preserve version identity
                # Only update dsl (for normalization consistency), description, and release
                update_data = {"dsl": normalized_dsl}
                if description is not None:
                    update_data["description"] = description
                if release is not None:
                    update_data["release"] = release
                cls.update_by_id(db, latest.id, update_data)
                cls.delete_all_versions(db, user_canvas_id)
                return latest.id, False

            insert_data = {"user_canvas_id": user_canvas_id, "dsl": normalized_dsl}
            if title is not None:
                insert_data["title"] = title
            if description is not None:
                insert_data["description"] = description
            if release is not None:
                insert_data["release"] = release
            cls.insert(db, **insert_data)
            cls.delete_all_versions(db, user_canvas_id)
            return None, True
        except Exception as e:
            logging.exception(e)
            return None, None
