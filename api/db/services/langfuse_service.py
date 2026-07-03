from datetime import datetime

from sqlalchemy.orm import Session

from api.db.db_models import TenantLangfuse
from api.db.services.common_service import CommonService
from common.time_utils import current_timestamp, datetime_format


class TenantLangfuseService(CommonService):
    """
    所有修改状态的方法都应该在事务中执行，以确保原子性并在执行过程中出现错误时维护数据完整性。
    """

    model = TenantLangfuse

    @classmethod
    def filter_by_tenant(cls, db: Session, tenant_id):
        """
        根据租户ID获取Langfuse配置信息。

        Args:
            db: 数据库会话对象。
            tenant_id: 租户ID。

        Returns:
            如果存在配置，返回配置对象；否则返回None。
        """
        fields = [cls.model.tenant_id, cls.model.host, cls.model.secret_key, cls.model.public_key]
        return db.query(*fields).filter(cls.model.tenant_id == tenant_id).first()

    @classmethod
    def filter_by_tenant_with_info(cls, db: Session, tenant_id):
        """
        根据租户ID获取Langfuse配置信息并以字典形式返回。

        Args:
            db: 数据库会话对象。
            tenant_id: 租户ID。

        Returns:
            如果存在配置，返回包含配置信息的字典；否则返回None。
        """
        fields = [cls.model.tenant_id, cls.model.host, cls.model.secret_key, cls.model.public_key]
        result = db.query(*fields).filter(cls.model.tenant_id == tenant_id).first()
        if result:
            # 将结果转换为字典形式
            return {
                'tenant_id': result[0],
                'host': result[1],
                'secret_key': result[2],
                'public_key': result[3]
            }
        return None

    @classmethod
    def delete_by_tenant_id(cls, db: Session, tenant_id: str) -> int:
        """根据tenant_id删除所有相关的Langfuse配置记录"""
        try:
            result = db.query(cls.model).filter(
                cls.model.tenant_id == tenant_id
            ).delete(synchronize_session=False)
            db.commit()
            return result
        except Exception as e:
            db.rollback()
            raise e

    @classmethod
    def update_by_tenant(cls, db: Session, tenant_id, langfuse_keys):
        """
        根据租户ID更新Langfuse配置信息。

        Args:
            db: 数据库会话对象。
            tenant_id: 租户ID。
            langfuse_keys: 更新的配置信息。

        Returns:
            更新操作的结果。
        """
        current_ts = current_timestamp()
        current_date = datetime_format(datetime.now())
        langfuse_keys["update_time"] = current_ts
        langfuse_keys["update_date"] = current_date

        result = db.query(cls.model).filter(cls.model.tenant_id == tenant_id).update(
            langfuse_keys,
            synchronize_session=False
        )
        db.commit()
        return result > 0

    @classmethod
    def save(cls, db: Session, **kwargs):
        """
        创建新的Langfuse配置记录。

        Args:
            db: 数据库会话对象。
            **kwargs: 配置信息。

        Returns:
            创建的配置对象。
        """
        current_ts = current_timestamp()
        current_date = datetime_format(datetime.now())

        kwargs["create_time"] = current_ts
        kwargs["create_date"] = current_date
        kwargs["update_time"] = current_ts
        kwargs["update_date"] = current_date

        obj = cls.model(**kwargs)
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj

    @classmethod
    def delete_model(cls, db: Session, langfuse_model):
        """
        删除指定的Langfuse配置模型。

        Args:
            db: 数据库会话对象。
            langfuse_model: 要删除的Langfuse模型实例。
        """
        db.delete(langfuse_model)
        db.commit()
