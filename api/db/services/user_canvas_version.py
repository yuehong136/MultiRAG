import json
import logging
import time
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from agent.dsl_migration import normalize_chunker_dsl
from api.db.db_models import UserCanvasVersion
from api.db.services.common_service import CommonService
from common.misc_utils import get_uuid


class UserCanvasVersionService(CommonService):
    model = UserCanvasVersion

    # Build a stable display name for saved snapshots.
    @staticmethod
    def build_version_title(user_nickname, agent_title, ts=None):
        tenant = str(user_nickname or "").strip() or "tenant"
        title = str(agent_title or "").strip() or "agent"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts is not None else time.strftime("%Y-%m-%d %H:%M:%S")
        return f"{tenant}_{title}_{stamp}"

    # Normalize DSL before comparing or writing version content.
    @staticmethod
    def _normalize_dsl(dsl: str | dict[str, Any]) -> dict[str, Any]:
        normalized = dsl
        if isinstance(normalized, str):
            try:
                normalized = json.loads(normalized)
            except Exception as e:
                raise ValueError("Invalid DSL JSON string.") from e

        if not isinstance(normalized, dict):
            raise ValueError("DSL must be a JSON object.")

        try:
            return json.loads(json.dumps(normalize_chunker_dsl(normalized), ensure_ascii=False))
        except Exception as e:
            raise ValueError("DSL is not JSON-serializable.") from e

    @classmethod
    def list_by_canvas_id(cls, db: Session, user_canvas_id: str):
        """Return all versions for the specified canvas ordered by newest first."""
        stmt = select(cls.model).where(cls.model.user_canvas_id == user_canvas_id).order_by(cls.model.create_time.desc())
        try:
            return db.execute(stmt).scalars().all()
        except Exception:
            logging.exception("Failed to list canvas versions for %s", user_canvas_id)
            return []

    @classmethod
    def get_all_canvas_version_by_canvas_ids(cls, db: Session, canvas_ids: list[str]):
        """根据canvas_ids批量查询所有版本ID，使用分页避免内存溢出"""
        stmt = select(cls.model.id).where(cls.model.user_canvas_id.in_(canvas_ids)).order_by(cls.model.create_time.asc())

        offset, limit = 0, 100
        res = []

        while True:
            try:
                version_batch = db.execute(stmt.offset(offset).limit(limit)).scalars().all()

                if not version_batch:
                    break

                res.extend([{"id": version_id} for version_id in version_batch])
                offset += limit
            except Exception:
                logging.exception("Failed to get canvas versions for batch at offset %d", offset)
                break

        return res

    @classmethod
    def delete_all_versions(cls, db: Session, user_canvas_id: str, *, commit: bool = True) -> bool:
        """Keep only the latest 20 unpublished versions and remove the rest. Released versions are always kept."""
        stmt = (
            select(cls.model.id)
            .where(
                cls.model.user_canvas_id == user_canvas_id,
                or_(cls.model.release == False, cls.model.release.is_(None)),
            )
            .order_by(cls.model.create_time.desc())
        )
        try:
            version_ids = db.execute(stmt).scalars().all()
            if len(version_ids) > 20:
                db.execute(delete(cls.model).where(cls.model.id.in_(version_ids[20:])))
                if commit:
                    db.commit()
            return True
        except Exception:
            if not commit:
                raise
            db.rollback()
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
    def save_or_replace_latest(
        cls,
        db: Session,
        user_canvas_id: str,
        dsl: str | dict[str, Any],
        title: str | None = None,
        description: str | None = None,
        release: bool | None = None,
        *,
        commit: bool = True,
    ) -> tuple[str | None, bool | None]:
        """Save a snapshot, protecting published content on a draft save.

        With commit=False the caller owns the transaction and errors propagate.
        Legacy callers retain the committed (version_id, created) result contract.
        """
        try:
            normalized_dsl = cls._normalize_dsl(dsl)
            latest = db.scalar(select(cls.model).where(cls.model.user_canvas_id == user_canvas_id).order_by(cls.model.create_time.desc()))
            replace = latest is not None and cls._normalize_dsl(latest.dsl) == normalized_dsl and not (latest.release and not release)
            timestamp = cls.current_timestamp()
            now = cls.current_datetime()
            if replace:
                version = latest
                timestamp = max(timestamp, version.create_time or 0, version.update_time or 0)
                version.dsl = normalized_dsl
                if description is not None:
                    version.description = description
                if release is not None:
                    version.release = release
                version.update_time = timestamp
                version.update_date = now
            else:
                # Even two saves in one millisecond must have a stable newest
                # snapshot. The agent update caller holds the canvas row lock.
                timestamp = max(timestamp, (latest.create_time or 0) + 1) if latest else timestamp
                version = cls.model(
                    id=get_uuid(),
                    user_canvas_id=user_canvas_id,
                    dsl=normalized_dsl,
                    title=title,
                    description=description,
                    release=release if release is not None else False,
                    create_time=timestamp,
                    create_date=now,
                    update_time=timestamp,
                    update_date=now,
                )
                db.add(version)
            db.flush()
            if not cls.delete_all_versions(db, user_canvas_id, commit=False):
                raise RuntimeError("Failed to trim canvas versions.")
            if commit:
                db.commit()
            return (version.id, False) if replace else (None, True)
        except Exception:
            if not commit:
                raise
            db.rollback()
            logging.exception("Failed to save canvas version for %s", user_canvas_id)
            return None, None
