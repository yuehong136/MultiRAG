# coding=utf-8
"""
@project: multirag
@Author：龙
@file： user_service.py
@date：2024/7/9 9:00
@desc:
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.exc import NoResultFound, IntegrityError
from sqlalchemy.orm import Session
from passlib.context import CryptContext

from api.db import UserTenantRole, StatusEnum
from api.db.db_models import User, Tenant, UserTenant
from api.db.services.common_service import CommonService
from api.utils import get_uuid, current_timestamp, datetime_format

# 创建密码上下文
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class UserService(CommonService):
    model = User

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
        if user and pwd_context.verify(password, user.password):
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
            kwargs["password"] = pwd_context.hash(str(kwargs["password"]))

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


class TenantService(CommonService):
    model = Tenant

    @classmethod
    def get_by_user_id(cls, db: Session, user_id: str):
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
        tenants = db.query(*fields).join(UserTenant, (cls.model.id == UserTenant.tenant_id)).filter(
            UserTenant.user_id == user_id,
            UserTenant.status == StatusEnum.VALID.value,
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
            UserTenant.role == UserTenantRole.NORMAL.value,
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


class UserTenantService(CommonService):
    model = UserTenant

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
                cls.model.user_id,
                cls.model.tenant_id,
                cls.model.role,
                cls.model.status,
                User.nickname,
                User.email,
                User.avatar,
                User.is_authenticated,
                User.is_active,
                User.is_anonymous,
                User.status,
                User.is_superuser,
            )
            .join(User, cls.model.user_id == User.id)
            .filter(
                cls.model.tenant_id == tenant_id,
                cls.model.status == StatusEnum.VALID.value
            )
        )
        results = query.all()
        users_data = [
            {
                "user_id": result[0],
                "tenant_id": result[1],
                "role": result[2],
                "status": result[3],
                "nickname": result[4],
                "email": result[5],
                "avatar": result[6],
                "is_authenticated": result[7],
                "is_active": result[8],
                "is_anonymous": result[9],
                "user_status": result[10],
                "is_superuser": result[11],
            }
            for result in results
        ]
        return users_data