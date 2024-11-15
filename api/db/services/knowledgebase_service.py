# coding=utf-8
"""
@project: multirag
@Author：龙
@file： knowledgebase_service.py
@date：2024/7/9 9:00
@desc:
"""
from sqlalchemy.orm import Session
from sqlalchemy.exc import NoResultFound
from api.db import StatusEnum, TenantPermission
from api.db.db_models import Knowledgebase, Tenant, User, UserTenant, Document
from api.db.services.common_service import CommonService


class KnowledgebaseService(CommonService):
    """
    知识库服务类，提供针对知识库的CRUD操作。
    """
    model = Knowledgebase

    @classmethod
    def list_documents_by_ids(cls, db: Session, kb_ids):
        doc_ids = db.query(Document.id.label("document_id")).filter(
            Document.kb_id.in_(kb_ids)
        ).all()

        # 提取查询结果中的文档ID并返回列表
        return [doc.document_id for doc in doc_ids]

    @classmethod
    def get_by_tenant_ids(cls, db: Session, joined_tenant_ids, user_id, page_number, items_per_page, orderby, desc):
        fields = [
            cls.model.id,
            cls.model.avatar,
            cls.model.name,
            cls.model.language,
            cls.model.description,
            cls.model.permission,
            cls.model.doc_num,
            cls.model.token_num,
            cls.model.chunk_num,
            cls.model.parser_id,
            cls.model.embd_id,
            User.nickname,
            User.avatar.label('tenant_avatar'),
            cls.model.update_time
        ]

        # 根据条件构建查询表达式
        query = db.query(*fields).join(User, cls.model.tenant_id == User.id).filter(
            ((cls.model.tenant_id.in_(joined_tenant_ids) & (cls.model.permission == TenantPermission.TEAM.value)) |
             (cls.model.tenant_id == user_id)) &
            (cls.model.status == StatusEnum.VALID.value)
        )

        # 根据desc参数确定排序方式
        if desc:
            query = query.order_by(getattr(cls.model, orderby).desc())
        else:
            query = query.order_by(getattr(cls.model, orderby).asc())

        # 分页查询并返回结果的字典形式
        kbs = query.offset((page_number - 1) * items_per_page).limit(items_per_page).all()
        # 将结果转换为字典列表
        result = []
        for kb in kbs:
            kb_dict = {
                'id': kb[0],
                'avatar': kb[1],
                'name': kb[2],
                'language': kb[3],
                'description': kb[4],
                'permission': kb[5],
                'doc_num': kb[6],
                'token_num': kb[7],
                'chunk_num': kb[8],
                'parser_id': kb[9],
                'embd_id': kb[10],
                'nickname': kb[11],
                'tenant_avatar': kb[12],
                'update_time': kb[13],
            }
            result.append(kb_dict)

        return result

    @classmethod
    def get_by_tenant_ids_by_offset(cls, db: Session, joined_tenant_ids, user_id, offset, count, orderby, desc):
        """
        根据租户ID列表和用户ID，通过偏移量和计数获取知识库列表。

        :param db: 数据库会话对象。
        :param joined_tenant_ids: 用户加入的租户ID列表。
        :param user_id: 用户ID。
        :param offset: 数据偏移量。
        :param count: 获取数据的数量。
        :param orderby: 排序字段。
        :param desc: 是否降序排序。
        :return: 符合条件的知识库列表的字典形式。
        """
        # 根据条件构建查询表达式
        query = db.query(cls.model).filter(
            ((cls.model.tenant_id.in_(joined_tenant_ids) & (cls.model.permission == TenantPermission.TEAM.value)) |
             (cls.model.tenant_id == user_id)) &
            (cls.model.status == StatusEnum.VALID.value)
        )

        # 根据desc参数确定排序方式
        if desc:
            query = query.order_by(getattr(cls.model, orderby).desc())
        else:
            query = query.order_by(getattr(cls.model, orderby).asc())

        # 获取所有匹配的数据
        kbs = query.all()
        kbs_length = len(kbs)

        # 检查偏移量是否有效
        if offset < 0 or offset >= kbs_length:
            raise IndexError("Offset is out of the valid range.")

        # 根据count参数返回数据子集的字典形式
        if count == -1:
            return [kb.to_dict() for kb in kbs[offset:]]
        return [kb.to_dict() for kb in kbs[offset:offset + count]]

    @classmethod
    def get_kb_ids(cls, db: Session, tenant_id):
        fields = [cls.model.id]
        kbs = db.query(*fields).filter(cls.model.tenant_id == tenant_id)
        kb_ids = [kb.id for kb in kbs]
        return kb_ids

    @classmethod
    def get_kb_names(cls, db: Session, tenant_id):
        fields = [cls.model.id]
        kbs = db.query(*fields).filter(cls.model.tenant_id == tenant_id)
        kb_names = [kb.name for kb in kbs]
        return kb_names

    @classmethod
    def get_detail(cls, db: Session, kb_id):
        """
        根据知识库ID获取详细信息。

        :param db: 数据库会话对象。
        :param kb_id: 知识库ID。
        :return: 知识库的详细信息字典，如果不存在则返回None。
        """
        # 定义查询的字段
        fields = [
            cls.model.id,
            cls.model.embd_id,
            cls.model.avatar,
            cls.model.name,
            cls.model.language,
            cls.model.description,
            cls.model.permission,
            cls.model.doc_num,
            cls.model.token_num,
            cls.model.chunk_num,
            cls.model.parser_id,
            cls.model.parser_config
        ]
        # 根据ID和状态查询知识库信息，并关联租户信息
        query = db.query(*fields).join(Tenant, (Tenant.id == cls.model.tenant_id)).filter(
            (cls.model.id == kb_id),
            (cls.model.status == StatusEnum.VALID.value),
            (Tenant.status == StatusEnum.VALID.value)
        ).first()

        # 返回查询结果的字典形式
        if not query:
            return None
        return {field.key: getattr(query, field.key) for field in fields}

    @classmethod
    def update_parser_config(cls, db: Session, id, config):
        """
        更新知识库的解析配置。

        :param db: 数据库会话对象。
        :param id: 知识库ID。
        :param config: 新的解析配置。
        """
        # 根据ID获取知识库实例
        kb = cls.get_by_id(db, id)
        if not kb:
            raise LookupError(f"knowledgebase({id}) not found.")

        # 递归更新解析配置
        def dfs_update(old, new):
            for k, v in new.items():
                if k not in old:
                    old[k] = v
                    continue
                if isinstance(v, dict):
                    assert isinstance(old[k], dict)
                    dfs_update(old[k], v)
                elif isinstance(v, list):
                    assert isinstance(old[k], list)
                    old[k] = list(set(old[k] + v))
                else:
                    old[k] = v

        dfs_update(kb.parser_config, config)
        # 更新知识库的解析配置
        cls.update_by_id(db, id, {"parser_config": kb.parser_config})

    @classmethod
    def get_field_map(cls, db: Session, ids):
        """
        根据知识库ID列表获取字段映射配置。

        :param db: 数据库会话对象。
        :param ids: 知识库ID列表。
        :return: 字段映射配置的合并结果。
        """
        conf = {}
        # 根据ID列表查询知识库，并提取字段映射配置
        kbs = cls.get_by_ids(db, ids)
        for k in kbs:
            if k.parser_config and "field_map" in k.parser_config:
                conf.update(k.parser_config["field_map"])
        return conf

    @classmethod
    def get_by_name(cls, db: Session, kb_name, tenant_id):
        """
        根据知识库名称和租户ID获取知识库实例。

        :param db: 数据库会话对象。
        :param kb_name: 知识库名称。
        :param tenant_id: 租户ID。
        :return: 如果知识库存在，返回(True, Knowledgebase实例)；否则返回(False, None)。
        """
        # 根据名称、租户ID和状态查询知识库
        kb = db.query(cls.model).filter(
            cls.model.name == kb_name,
            cls.model.tenant_id == tenant_id,
            cls.model.status == StatusEnum.VALID.value
        ).first()
        if kb:
            return True, kb
        return False, None

    @classmethod
    def get_all_ids(cls, db: Session):
        """
        获取所有知识库的ID列表。

        :param db: 数据库会话对象。
        :return: 所有知识库ID的列表。
        """
        ids = db.query(cls.model.id).all()
        return [id[0] for id in ids]

    @classmethod
    def get_list(cls, db: Session, joined_tenant_ids, user_id, page_number, items_per_page, orderby, desc, id, name):
        """
        根据租户ID列表、用户ID、知识库ID和名称获取知识库列表。

        :param db: 数据库会话对象。
        :param joined_tenant_ids: 用户加入的租户ID列表。
        :param user_id: 用户ID。
        :param page_number: 页码。
        :param items_per_page: 每页项数。
        :param orderby: 排序字段。
        :param desc: 是否降序排序。
        :param id: 知识库ID（可选）。
        :param name: 知识库名称（可选）。
        :return: 符合条件的知识库列表的字典形式。
        """
        # 构建基础查询条件
        query = db.query(cls.model).filter(
            ((cls.model.tenant_id.in_(joined_tenant_ids) & (cls.model.permission == TenantPermission.TEAM.value)) |
             (cls.model.tenant_id == user_id)) &
            (cls.model.status == StatusEnum.VALID.value)
        )

        # 根据ID和名称进行进一步过滤（如果有提供）
        if id:
            query = query.filter(cls.model.id == id)
        if name:
            query = query.filter(cls.model.name == name)

        # 根据desc参数确定排序方式
        if desc:
            query = query.order_by(getattr(cls.model, orderby).desc())
        else:
            query = query.order_by(getattr(cls.model, orderby).asc())

        # 分页查询并返回结果的字典形式
        kbs = query.offset((page_number - 1) * items_per_page).limit(items_per_page).all()
        return [kb.to_dict() for kb in kbs]

    @classmethod
    def accessible(cls, db: Session, kb_id, user_id):
        docs = db.query(cls.model.id).join(
            UserTenant, UserTenant.tenant_id == Knowledgebase.tenant_id
        ).filter(
            cls.model.id == kb_id,
            UserTenant.user_id == user_id
        ).limit(1).all()

        if not docs:
            return False
        return True

    @classmethod
    def get_kb_by_id(cls, db: Session, kb_id, user_id):
        query = db.query(cls.model).join(UserTenant, UserTenant.tenant_id == cls.model.tenant_id).filter(
            cls.model.id == kb_id,
            UserTenant.user_id == user_id
        ).limit(1)

        return [kb.to_dict() for kb in query]

    @classmethod
    def get_kb_by_name(cls, db: Session, kb_name, user_id):
        query = db.query(cls.model).join(UserTenant, UserTenant.tenant_id == cls.model.tenant_id).filter(
            cls.model.name == kb_name,
            UserTenant.user_id == user_id
        ).limit(1)

        return [kb.to_dict() for kb in query]

    @classmethod
    def accessible4deletion(cls, db: Session, kb_id, user_id):
        docs = db.query(cls.model.id).filter(
            cls.model.id == kb_id,
            cls.model.created_by == user_id
        ).limit(1).all()

        if not docs:
            return False
        return True

