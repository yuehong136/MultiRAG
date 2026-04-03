import hashlib
import logging
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.exc import NoResultFound, IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import func, select, update
import bcrypt

from api.db import UserTenantRole
from api.db.db_models import User, Tenant, UserTenant
from api.db.services.common_service import CommonService
from common import settings
from common.constants import StatusEnum
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp, datetime_format


class UserService(CommonService):
    model = User

    @staticmethod
    def hash_password(password: str) -> str:
        # 生成盐值
        salt = bcrypt.gensalt()
        # 哈希密码
        hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
        # 返回哈希后的密码，解码为字符串
        return hashed.decode('utf-8')

    @staticmethod
    def verify_password(password: str, hashed_password: str) -> bool:
        # 验证密码
        return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))

    @classmethod
    def query(cls, db: Session, cols: list[str] | None = None, reverse: bool | None = None, order_by: str | None = None, **kwargs):
        if 'access_token' in kwargs:
            access_token = kwargs['access_token']

            # Reject empty, None, or whitespace-only access tokens
            if not access_token or not str(access_token).strip():
                logging.warning("UserService.query: Rejecting empty access_token query")
                return db.query(cls.model).filter(cls.model.id == "INVALID_EMPTY_TOKEN")  # Returns empty result

            # Reject tokens that are too short (should be UUID, 32+ chars)
            if len(str(access_token).strip()) < 32:
                logging.warning(f"UserService.query: Rejecting short access_token query: {len(str(access_token))} chars")
                return db.query(cls.model).filter(cls.model.id == "INVALID_SHORT_TOKEN")  # Returns empty result

            # Reject tokens that start with "INVALID_" (from logout)
            if str(access_token).startswith("INVALID_"):
                logging.warning("UserService.query: Rejecting invalidated access_token")
                return db.query(cls.model).filter(cls.model.id == "INVALID_LOGOUT_TOKEN")  # Returns empty result

        # Call parent query method for valid requests
        return super().query(db=db, cols=cols, reverse=reverse, order_by=order_by, **kwargs)

    @classmethod
    def filter_by_id(cls, db: Session, user_id: str):
        """通过主键查询用户（使用 session.get() 主键直取）"""
        return db.get(cls.model, user_id)

    @classmethod
    def query_user(cls, db: Session, email: str, password: str):
        stmt = select(cls.model).where(
            cls.model.email == email,
            cls.model.status == StatusEnum.VALID.value
        )
        user = db.scalars(stmt).first()
        if user and cls.verify_password(password, user.password):
            return user
        return None

    @classmethod
    def query_user_onlywith_email(cls, db: Session, email: str):
        stmt = select(cls.model).where(
            cls.model.email == email,
            cls.model.status == StatusEnum.VALID.value
        )
        return db.scalars(stmt).first()

    @classmethod
    def query_user_by_email(cls, db: Session, email: str):
        """根据邮箱查询用户列表"""
        stmt = select(cls.model).where(cls.model.email == email)
        return db.scalars(stmt).all()

    @classmethod
    def save(cls, db: Session, **kwargs):
        if "id" not in kwargs:
            kwargs["id"] = get_uuid()
        if "password" in kwargs:
            # kwargs["password"] = pwd_context.hash(str(kwargs["password"]))
            kwargs["password"] = cls.hash_password(str(kwargs["password"]))
        current_ts = current_timestamp()
        current_date = datetime_format(datetime.now())

        kwargs["create_time"] = current_ts
        kwargs["create_date"] = current_date
        kwargs["update_time"] = current_ts
        kwargs["update_date"] = current_date

        user = cls.model(**kwargs)
        db.add(user)
        try:
            db.commit()
            db.refresh(user)
        except IntegrityError as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Integrity error: {str(e)}")
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")
        return user

    @classmethod
    def delete_user(cls, db: Session, user_ids: list, update_user_dict: dict):
        try:
            stmt = update(cls.model).where(cls.model.id.in_(user_ids)).values(status="0")
            db.execute(stmt)
            db.commit()
        except Exception as e:
            db.rollback()
            raise e

    @classmethod
    def update_user(cls, db: Session, user_id: str, user_dict: dict):
        try:
            if user_dict:
                current_ts = current_timestamp()
                current_date = datetime_format(datetime.now())
                user_dict["update_time"] = current_ts
                user_dict["update_date"] = current_date
                stmt = update(cls.model).where(cls.model.id == user_id).values(**user_dict)
                db.execute(stmt)
                db.commit()
        except Exception as e:
            db.rollback()
            raise e

    @classmethod
    def update_user_password(cls, db: Session, user_id: str, new_password: str):
        """更新用户密码"""
        try:
            current_ts = current_timestamp()
            current_date = datetime_format(datetime.now())
            stmt = update(cls.model).where(cls.model.id == user_id).values(
                password=cls.hash_password(str(new_password)),
                update_time=current_ts,
                update_date=current_date
            )
            db.execute(stmt)
            db.commit()
        except Exception as e:
            db.rollback()
            raise e

    @classmethod
    def is_admin(cls, db: Session, user_id):
        stmt = select(func.count()).select_from(cls.model).where(
            cls.model.id == user_id,
            cls.model.is_superuser == 1
        )
        return db.scalar(stmt) > 0

    @classmethod
    def get_all_users(cls, db: Session):
        stmt = select(cls.model).order_by(cls.model.email)
        return db.scalars(stmt).all()


class TenantService(CommonService):
    model = Tenant

    @classmethod
    def get_info_by(cls, db: Session, user_id: str):
        stmt = select(
            cls.model.id.label("tenant_id"),
            cls.model.name,
            cls.model.llm_id,
            cls.model.tenant_llm_id,
            cls.model.embd_id,
            cls.model.tenant_embd_id,
            cls.model.rerank_id,
            cls.model.tenant_rerank_id,
            cls.model.asr_id,
            cls.model.tenant_asr_id,
            cls.model.img2txt_id,
            cls.model.tenant_img2txt_id,
            cls.model.tts_id,
            cls.model.tenant_tts_id,
            cls.model.parser_ids,
            UserTenant.role.label("role")
        ).join(
            UserTenant,
            cls.model.id == UserTenant.tenant_id
        ).where(
            UserTenant.user_id == user_id,
            UserTenant.status == StatusEnum.VALID.value,
            UserTenant.role == UserTenantRole.OWNER,
            cls.model.status == StatusEnum.VALID.value
        )
        tenants = db.execute(stmt).all()

        return [
            {
                "tenant_id": tenant.tenant_id,
                "name": tenant.name,
                "llm_id": tenant.llm_id,
                "tenant_llm_id": tenant.tenant_llm_id,
                "embd_id": tenant.embd_id,
                "tenant_embd_id": tenant.tenant_embd_id,
                "rerank_id": tenant.rerank_id,
                "tenant_rerank_id": tenant.tenant_rerank_id,
                "asr_id": tenant.asr_id,
                "tenant_asr_id": tenant.tenant_asr_id,
                "img2txt_id": tenant.img2txt_id,
                "tenant_img2txt_id": tenant.tenant_img2txt_id,
                "tts_id": tenant.tts_id,
                "tenant_tts_id": tenant.tenant_tts_id,
                "parser_ids": tenant.parser_ids,
                "role": tenant.role
            }
            for tenant in tenants
        ]

    @classmethod
    def get_null_tenant_model_id_rows(cls, db: Session):
        stmt = select(
            cls.model.id,
            cls.model.llm_id,
            cls.model.embd_id,
            cls.model.asr_id,
            cls.model.img2txt_id,
            cls.model.rerank_id,
            cls.model.tts_id,
        ).where(
            (cls.model.tenant_llm_id.is_(None))
            | (cls.model.tenant_embd_id.is_(None))
            | (cls.model.tenant_asr_id.is_(None))
            | (cls.model.tenant_img2txt_id.is_(None))
            | (cls.model.tenant_rerank_id.is_(None))
            | (cls.model.tenant_tts_id.is_(None))
        )
        return db.execute(stmt).all()

    @classmethod
    def get_joined_tenants_by_user_id(cls, db: Session, user_id: str):
        stmt = select(
            cls.model.id.label("tenant_id"),
            cls.model.name,
            cls.model.llm_id,
            cls.model.embd_id,
            cls.model.asr_id,
            cls.model.img2txt_id,
            UserTenant.role
        ).join(
            UserTenant, cls.model.id == UserTenant.tenant_id
        ).where(
            UserTenant.user_id == user_id,
            UserTenant.status == StatusEnum.VALID.value,
            UserTenant.role.in_([UserTenantRole.NORMAL, UserTenantRole.ADMIN]),
            cls.model.status == StatusEnum.VALID.value
        )
        return db.execute(stmt).all()

    @classmethod
    def decrease(cls, db: Session, user_id: str, num: int):
        stmt = update(cls.model).where(cls.model.id == user_id).values(
            credit=cls.model.credit - num
        )
        result = db.execute(stmt)
        db.commit()
        if result.rowcount == 0:
            raise LookupError("Tenant not found which is supposed to be there")

    @classmethod
    def user_gateway(cls, db: Session, tenant_id):
        hash_obj = hashlib.sha256(tenant_id.encode("utf-8"))
        return int(hash_obj.hexdigest(), 16)%len(settings.MINIO)


class UserTenantService(CommonService):
    model = UserTenant

    @staticmethod
    def can_access_tenant_resources(role: str | None) -> bool:
        return role == UserTenantRole.OWNER or UserTenantService.is_joined_member(role)

    @staticmethod
    def is_joined_member(role: str | None) -> bool:
        return role in {UserTenantRole.NORMAL, UserTenantRole.ADMIN}

    @staticmethod
    def can_manage_members(role: str | None) -> bool:
        return role in {UserTenantRole.OWNER, UserTenantRole.ADMIN}

    @staticmethod
    def can_manage_roles(role: str | None) -> bool:
        return role == UserTenantRole.OWNER

    @classmethod
    def filter_by_id(cls, db: Session, user_tenant_id: str):
        """根据user_tenant_id查找有效的UserTenant记录"""
        try:
            stmt = select(cls.model).where(
                cls.model.id == user_tenant_id,
                cls.model.status == StatusEnum.VALID.value
            )
            return db.scalars(stmt).one()
        except NoResultFound:
            return None

    @classmethod
    def save(cls, db: Session, **kwargs):
        if "id" not in kwargs:
            kwargs["id"] = get_uuid()
        user_tenant = cls.model(**kwargs)
        db.add(user_tenant)
        try:
            db.commit()
        except IntegrityError as e:
            db.rollback()
            raise e
        db.refresh(user_tenant)
        return user_tenant

    @classmethod
    def get_by_tenant_id(cls, db: Session, tenant_id):
        stmt = select(
            cls.model.id,
            cls.model.user_id,
            cls.model.status,
            cls.model.role,
            User.nickname,
            User.email,
            User.avatar,
            User.is_authenticated,
            User.is_active,
            User.is_anonymous,
            User.status,
            User.update_date,
            User.is_superuser,
        ).join(
            User, cls.model.user_id == User.id
        ).where(
            cls.model.tenant_id == tenant_id,
            cls.model.status == StatusEnum.VALID.value,
            cls.model.role != UserTenantRole.OWNER
        )
        results = db.execute(stmt).all()
        return [
            {
                "id": result[0],
                "user_id": result[1],
                "role": result[3],
                "nickname": result[4],
                "email": result[5],
                "avatar": result[6],
                "is_authenticated": result[7],
                "is_active": result[8],
                "is_anonymous": result[9],
                "status": result[10],  # User.status，和 ragflow 一致
                "update_date": result[11],
                "is_superuser": result[12],
            }
            for result in results
        ]

    @classmethod
    def get_tenants_by_user_id(cls, db: Session, user_id: str):
        stmt = select(
            cls.model.tenant_id.label("tenant_id"),
            cls.model.role.label("role"),
            User.nickname,
            User.email,
            User.avatar,
            User.update_date
        ).join(
            User, cls.model.tenant_id == User.id
        ).where(
            cls.model.user_id == user_id,
            cls.model.status == StatusEnum.VALID.value
        )
        tenants = db.execute(stmt).all()

        return [
            {
                "tenant_id": tenant.tenant_id,
                "role": tenant.role,
                "nickname": tenant.nickname,
                "email": tenant.email,
                "avatar": tenant.avatar,
                "update_date": tenant.update_date
            }
            for tenant in tenants
        ]

    @classmethod
    def get_user_tenant_relation_by_user_id(cls, db: Session, user_id: str):
        """根据user_id查询用户租户关系"""
        stmt = select(
            cls.model.id,
            cls.model.user_id,
            cls.model.tenant_id,
            cls.model.role
        ).where(cls.model.user_id == user_id)
        results = db.execute(stmt).all()

        return [
            {
                "id": result.id,
                "user_id": result.user_id,
                "tenant_id": result.tenant_id,
                "role": result.role
            }
            for result in results
        ]

    @classmethod
    def get_num_members(cls, db: Session, user_id: str):
        """获取指定租户的成员数量"""
        stmt = select(func.count(cls.model.id)).where(cls.model.tenant_id == user_id)
        return db.scalar(stmt)

    @classmethod
    def filter_by_tenant_and_user_id(cls, db: Session, tenant_id: str, user_id: str):
        """根据tenant_id和user_id查找有效的UserTenant记录"""
        stmt = select(cls.model).where(
            cls.model.tenant_id == tenant_id,
            cls.model.status == StatusEnum.VALID.value,
            cls.model.user_id == user_id
        )
        return db.scalars(stmt).first()

    @classmethod
    def get_membership(cls, db: Session, tenant_id: str, user_id: str):
        return cls.filter_by_tenant_and_user_id(db, tenant_id=tenant_id, user_id=user_id)
