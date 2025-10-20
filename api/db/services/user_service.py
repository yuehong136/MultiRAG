# coding=utf-8
"""
@project: multirag
@Author：龙
@file： user_service.py
@date：2024/7/9 9:00
@desc:
"""
import hashlib
import logging
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.exc import NoResultFound, IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import func
# from passlib.context import CryptContext
import bcrypt

from api.db import UserTenantRole, StatusEnum
from api.db.db_models import User, Tenant, UserTenant
from api.db.services.common_service import CommonService
from api.utils import get_uuid, current_timestamp, datetime_format
from core.settings import MINIO


# # 创建密码上下文
# pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

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
        try:
            user = db.query(cls.model).filter(cls.model.id == user_id).one()
            return user
        except NoResultFound:
            return None

    @classmethod
    def query_user(cls, db: Session, email: str, password: str):
        user = db.query(cls.model).filter(
            cls.model.email == email,
            cls.model.status == StatusEnum.VALID.value
        ).first()
        # if user and pwd_context.verify(password, user.password):
        if user and cls.verify_password(password, user.password):
            return user
        else:
            return None

    @classmethod
    def query_user_onlywith_email(cls, db: Session, email: str):
        user = db.query(cls.model).filter(
            cls.model.email == email,
            cls.model.status == StatusEnum.VALID.value
        ).first()
        if user:
            return user
        else:
            return None

    @classmethod
    def save(cls, db: Session, **kwargs):
        if "id" not in kwargs:
            kwargs["id"] = get_uuid()
        if "password" in kwargs:
            # kwargs["password"] = pwd_context.hash(str(kwargs["password"]))
            kwargs["password"] = cls.hash_password(str(kwargs["password"]))
        kwargs["create_time"] = current_timestamp()
        kwargs["create_date"] = datetime_format(datetime.now())
        kwargs["update_time"] = current_timestamp()
        kwargs["update_date"] = datetime_format(datetime.now())

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
            db.query(cls.model).filter(cls.model.id.in_(user_ids)).update(
                {"status": 0},
                synchronize_session=False
            )
            db.commit()
        except Exception as e:
            db.rollback()
            raise e

    @classmethod
    def update_user(cls, db: Session, user_id: str, user_dict: dict):
        try:
            if user_dict:
                user_dict["update_time"] = current_timestamp()
                user_dict["update_date"] = datetime_format(datetime.now())
                db.query(cls.model).filter(cls.model.id == user_id).update(
                    user_dict,
                    synchronize_session=False
                )
                db.commit()
        except Exception as e:
            db.rollback()
            raise e

    @classmethod
    def is_admin(cls, db: Session, user_id):
        return db.query(cls.model).filter(
            cls.model.id == user_id,
            cls.model.is_superuser == 1).count() > 0


class TenantService(CommonService):
    model = Tenant

    @classmethod
    def get_info_by(cls, db: Session, user_id: str):
        fields = [
            cls.model.id.label("tenant_id"),
            cls.model.name,
            cls.model.llm_id,
            cls.model.embd_id,
            cls.model.rerank_id,
            cls.model.asr_id,
            cls.model.img2txt_id,
            cls.model.tts_id,
            cls.model.parser_ids,
            UserTenant.role.label("role")
        ]
        tenants = db.query(*fields).join(
            UserTenant,
            (cls.model.id == UserTenant.tenant_id)
        ).filter(
            UserTenant.user_id == user_id,
            UserTenant.status == StatusEnum.VALID.value,
            UserTenant.role == UserTenantRole.OWNER,
            cls.model.status == StatusEnum.VALID.value
        ).all()

        tenant_list = []
        for tenant in tenants:
            tenant_dict = {
                "tenant_id": tenant.tenant_id,
                "name": tenant.name,
                "llm_id": tenant.llm_id,
                "embd_id": tenant.embd_id,
                "rerank_id": tenant.rerank_id,
                "asr_id": tenant.asr_id,
                "img2txt_id": tenant.img2txt_id,
                "tts_id": tenant.tts_id,
                "parser_ids": tenant.parser_ids,
                "role": tenant.role
            }
            tenant_list.append(tenant_dict)

        return tenant_list

    @classmethod
    def get_joined_tenants_by_user_id(cls, db: Session, user_id: str):
        fields = [
            cls.model.id.label("tenant_id"),
            cls.model.name,
            cls.model.llm_id,
            cls.model.embd_id,
            cls.model.asr_id,
            cls.model.img2txt_id,
            UserTenant.role
        ]
        return db.query(*fields).join(UserTenant, (cls.model.id == UserTenant.tenant_id)).filter(
            UserTenant.user_id == user_id,
            UserTenant.status == StatusEnum.VALID.value,
            UserTenant.role == UserTenantRole.NORMAL,
            cls.model.status == StatusEnum.VALID.value
        ).all()

    @classmethod
    def decrease(cls, db: Session, user_id: str, num: int):
        result = db.query(cls.model).filter(cls.model.id == user_id).update(
            {"credit": cls.model.credit - num},
            synchronize_session=False
        )
        db.commit()
        if result == 0:
            raise LookupError("Tenant not found which is supposed to be there")

    @classmethod
    def user_gateway(cls, db: Session, tenant_id):
        hashobj = hashlib.sha256(tenant_id.encode("utf-8"))
        return int(hashobj.hexdigest(), 16)%len(MINIO)


class UserTenantService(CommonService):
    model = UserTenant

    @classmethod
    def filter_by_id(cls, db: Session, user_tenant_id: str):
        """根据user_tenant_id查找有效的UserTenant记录"""
        try:
            user_tenant = db.query(cls.model).filter(
                cls.model.id == user_tenant_id,
                cls.model.status == StatusEnum.VALID.value
            ).one()
            return user_tenant
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
        query = (
            db.query(
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
            )
            .join(User, cls.model.user_id == User.id)
            .filter(
                cls.model.tenant_id == tenant_id,
                cls.model.status == StatusEnum.VALID.value,
                cls.model.role != UserTenantRole.OWNER
            )
        )
        results = query.all()
        users_data = [
            {
                "user_id": result[0],
                "status": result[1],
                "role": result[2],
                "nickname": result[3],
                "email": result[4],
                "avatar": result[5],
                "is_authenticated": result[6],
                "is_active": result[7],
                "is_anonymous": result[8],
                "user_status": result[9],
                "update_date": result[10],
                "is_superuser": result[11],
            }
            for result in results
        ]
        return users_data

    @classmethod
    def get_tenants_by_user_id(cls, db: Session, user_id: str):
        fields = [
            cls.model.tenant_id.label("tenant_id"),
            cls.model.role.label("role"),
            User.nickname,
            User.email,
            User.avatar,
            User.update_date
        ]

        tenants = (
            db.query(*fields)
            .join(User, cls.model.tenant_id == User.id)
            .filter(
                cls.model.user_id == user_id,
                cls.model.status == StatusEnum.VALID.value,
                UserTenant.status == StatusEnum.VALID.value
            )
            .all()
        )

        tenant_list = [
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

        return tenant_list

    @classmethod
    def get_num_members(cls, db: Session, user_id: str):
        """获取指定租户的成员数量"""
        cnt_members = db.query(func.count(cls.model.id)).filter(
            cls.model.tenant_id == user_id
        ).scalar()
        return cnt_members

    @classmethod
    def filter_by_tenant_and_user_id(cls, db: Session, tenant_id: str, user_id: str):
        """根据tenant_id和user_id查找有效的UserTenant记录"""
        try:
            user_tenant = db.query(cls.model).filter(
                cls.model.tenant_id == tenant_id,
                cls.model.status == StatusEnum.VALID.value,
                cls.model.user_id == user_id
            ).first()
            return user_tenant
        except NoResultFound:
            return None