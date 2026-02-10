from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from api.db.db_models import SystemSettings
from api.db.services.common_service import CommonService
from common.time_utils import current_timestamp, datetime_format


class SystemSettingsService(CommonService):
    model = SystemSettings

    def __init__(self):
        super().__init__(SystemSettings)

    @classmethod
    def get_by_name(cls, db: Session, name: str) -> list[SystemSettings]:
        stmt = select(cls.model).where(cls.model.name.startswith(name))
        return list(db.scalars(stmt).all())

    @classmethod
    def update_by_name(cls, db: Session, name: str, data: dict) -> SystemSettings:
        data["update_time"] = current_timestamp()
        data["update_date"] = datetime_format(datetime.now())
        stmt = update(cls.model).where(cls.model.name.startswith(name)).values(data)
        db.execute(stmt)
        db.commit()
        return SystemSettings(**data)

    @classmethod
    def get_record_count(cls, db: Session) -> int:
        stmt = select(cls.model)
        return len(list(db.scalars(stmt).all()))
