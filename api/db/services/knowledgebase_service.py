# coding=utf-8
"""
@project: multirag
@Author：龙
@file： knowledgebase_service.py
@date：2024/7/9 9:00
@desc:
"""
from datetime import datetime

from sqlalchemy import func, update, or_, and_
from sqlalchemy.orm import Session
from api.db import StatusEnum, TenantPermission
from api.db.db_models import Knowledgebase, Tenant, User, UserTenant, Document, UserCanvas
from api.db.services.common_service import CommonService
from common.time_utils import current_timestamp, datetime_format
from api.db.services.user_service import TenantService
from common.misc_utils import get_uuid
from api.constants import DATASET_NAME_LIMIT


class KnowledgebaseService(CommonService):
    """
    知识库服务类，提供针对知识库的CRUD操作。
    """
    model = Knowledgebase

    @classmethod
    def is_parsed_done(cls, db, kb_id):
        """
        Check if all documents in the knowledge base have completed parsing

        Args:
            kb_id: Knowledge base ID

        Returns:
            If all documents are parsed successfully, returns (True, None)
            If any document is not fully parsed, returns (False, error_message)
        """
        from api.db import TaskStatus
        from api.db.services.document_service import DocumentService

        # Get knowledge base information
        kbs = cls.query(db, id=kb_id)
        if not kbs:
            return False, "Knowledge base not found"
        kb = kbs[0]

        # Get all documents in the knowledge base
        # docs, _ = DocumentService.get_by_kb_id(db, kb_id, 1, 1000, "create_time", True, "")
        docs, _ = DocumentService.get_by_kb_id(db, kb_id, 1, 1000, "create_time", True, "", [], [])

        # Check parsing status of each document
        for doc in docs:
            # If document is being parsed, don't allow chat creation
            if doc['run'] == TaskStatus.RUNNING.value or doc['run'] == TaskStatus.CANCEL.value or doc[
                'run'] == TaskStatus.FAIL.value:
                return False, f"Document '{doc['name']}' in dataset '{kb.name}' is still being parsed. Please wait until all documents are parsed before starting a chat."
            # If document is not yet parsed and has no chunks, don't allow chat creation
            if doc['run'] == TaskStatus.UNSTART.value and doc['chunk_num'] == 0:
                return False, f"Document '{doc['name']}' in dataset '{kb.name}' has not been parsed yet. Please parse all documents before starting a chat."

        return True, None

    @classmethod
    def list_documents_by_ids(cls, db: Session, kb_ids):
        doc_ids = db.query(Document.id.label("document_id")).filter(
            Document.kb_id.in_(kb_ids)
        ).all()

        # 提取查询结果中的文档ID并返回列表
        return [doc.document_id for doc in doc_ids]

    @classmethod
    def get_by_tenant_ids(cls, db: Session, joined_tenant_ids, user_id, page_number, items_per_page, orderby, desc,
                          keywords=None, parser_id=None):
        fields = [
            cls.model.id,
            cls.model.avatar,
            cls.model.name,
            cls.model.language,
            cls.model.description,
            cls.model.tenant_id,
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

        # 构建查询表达式
        query = db.query(*fields).join(User, cls.model.tenant_id == User.id).filter(
            ((cls.model.tenant_id.in_(joined_tenant_ids) & (cls.model.permission == TenantPermission.TEAM.value)) |
             (cls.model.tenant_id == user_id)) &
            (cls.model.status == StatusEnum.VALID.value)
        )
        if parser_id:
            query = query.filter(cls.model.parser_id == parser_id)
        # 如果提供了关键词，则增加模糊搜索条件
        if keywords:
            query = query.filter(func.lower(cls.model.name).ilike(f"%{keywords}%"))
        # 根据desc参数确定排序方式
        if desc:
            query = query.order_by(getattr(cls.model, orderby).desc())
        else:
            query = query.order_by(getattr(cls.model, orderby).asc())

        # 获取总记录数
        total = query.count()

        # 条件分页：当设置了每页数量时执行分页，page_number<=0 时回退为第1页
        if items_per_page:
            page_number = max(1, int(page_number) if page_number is not None else 1)
            kbs = query.offset((page_number - 1) * items_per_page).limit(items_per_page).all()
        else:
            kbs = query.all()

        # 将结果转换为字典列表
        result = []
        for kb in kbs:
            kb_dict = {
                'id': kb[0],
                'avatar': kb[1],
                'name': kb[2],
                'language': kb[3],
                'description': kb[4],
                'tenant_id': kb[5],
                'permission': kb[6],
                'doc_num': kb[7],
                'token_num': kb[8],
                'chunk_num': kb[9],
                'parser_id': kb[10],
                'embd_id': kb[11],
                'nickname': kb[12],
                'tenant_avatar': kb[13],
                'update_time': kb[14],
            }
            result.append(kb_dict)

        # 返回结果和总记录数
        return result, total

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
    def get_all_kb_by_tenant_ids(cls, db: Session, tenant_ids: list, user_id: str):
        """
        根据租户ID列表获取所有有权限的知识库
        
        Args:
            db: 数据库会话
            tenant_ids: 租户ID列表
            user_id: 用户ID
            
        Returns:
            list: 知识库字典列表
        """
        # will get all permitted kb, be cautious.
        fields = [
            cls.model.name,
            cls.model.language,
            cls.model.permission,
            cls.model.doc_num,
            cls.model.token_num,
            cls.model.chunk_num,
            cls.model.status,
            cls.model.create_date,
            cls.model.update_date
        ]
        # find team kb and owned kb
        query = db.query(*fields).filter(
            or_(
                and_(
                    cls.model.tenant_id.in_(tenant_ids),
                    cls.model.permission == TenantPermission.TEAM.value
                ),
                cls.model.tenant_id == user_id
            )
        ).order_by(cls.model.create_time.asc())
        # maybe cause slow query by deep paginate, optimize later.
        offset, limit = 0, 50
        res = []
        while True:
            kb_batch = query.offset(offset).limit(limit).all()
            if not kb_batch:
                break
            # 将查询结果转换为字典
            for kb in kb_batch:
                res.append({
                    "name": kb.name,
                    "language": kb.language,
                    "permission": kb.permission,
                    "doc_num": kb.doc_num,
                    "token_num": kb.token_num,
                    "chunk_num": kb.chunk_num,
                    "status": kb.status,
                    "create_date": kb.create_date,
                    "update_date": kb.update_date
                })
            offset += limit
        return res

    @classmethod
    def get_kb_ids(cls, db: Session, tenant_id):
        fields = [cls.model.id]
        kbs = db.query(*fields).filter(cls.model.tenant_id == tenant_id)
        kb_ids = [kb.id for kb in kbs]
        return kb_ids

    @classmethod
    def get_kb_names(cls, db: Session, tenant_id):
        fields = [cls.model.name]
        kbs = db.query(*fields).filter(cls.model.tenant_id == tenant_id)
        kb_names = [kb.name for kb in kbs]
        return kb_names

    @classmethod
    def get_detail(cls, db: Session, kb_id):
        """
        Get detailed information about a knowledge base

        Args:
            db: Database session object
            kb_id: Knowledge base ID

        Returns:
            Dictionary containing knowledge base details, or None if not found
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
            cls.model.pipeline_id,
            UserCanvas.title.label("pipeline_name"),
            UserCanvas.avatar.label("pipeline_avatar"),
            cls.model.parser_config,
            cls.model.pagerank,
            cls.model.graphrag_task_id,
            cls.model.graphrag_task_finish_at,
            cls.model.raptor_task_id,
            cls.model.raptor_task_finish_at,
            cls.model.mindmap_task_id,
            cls.model.mindmap_task_finish_at,
            cls.model.create_time,
            cls.model.update_time
        ]

        # LEFT JOIN UserCanvas to get pipeline information
        query = (
            db.query(*fields)
            .outerjoin(UserCanvas, cls.model.pipeline_id == UserCanvas.id)
            .filter(
                cls.model.id == kb_id,
                cls.model.status == StatusEnum.VALID.value
            )
            .first()
        )

        # 返回查询结果的字典形式
        if not query:
            return None

        # 使用 Row._mapping 转换为字典
        # 这会保留所有字段的键名，包括 .label() 定义的别名
        return dict(query._mapping)  # todo 用dict(query._mapping)替换原先手动访问所有字段方案，目前只在此处做了

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
    def delete_field_map(cls, db: Session, id: str):
        """
        删除知识库的字段映射配置。

        :param db: 数据库会话对象。
        :param id: 知识库ID。
        """
        # 根据ID获取知识库实例
        kb = cls.get_by_id(db, id)
        if not kb:
            raise LookupError(f"knowledgebase({id}) not found.")

        # 从parser_config中删除field_map字段
        kb.parser_config.pop("field_map", None)
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
    def create_with_name(
        cls,
        db: Session,
        *,
        name: str,
        tenant_id: str,
        parser_id: str | None = None,
        embd_id: str | None = None,
        parser_config: dict | None = None,
        **kwargs
    ):
        """Create a dataset (knowledgebase) by name with kb_app defaults.

        This encapsulates the creation logic used in kb_app.create so other callers
        (including RESTFul endpoints) can reuse the same behavior.

        Args:
            db: Database session
            name: Dataset name
            tenant_id: Tenant ID
            parser_id: Parser ID (chunk method)
            embd_id: Embedding model ID (if None, uses tenant's default)
            parser_config: Parser configuration (must be provided by caller)
            **kwargs: Other fields (description, permission, etc.)

        Returns:
            dict: payload dictionary for creating knowledgebase
        
        Raises:
            ValueError: If validation fails
        """
        # Validate name (basic checks, MILVUS pattern check should be done by caller)
        if not isinstance(name, str):
            raise ValueError("Dataset name must be string.")
        dataset_name = name.strip()
        if dataset_name == "":
            raise ValueError("Dataset name can't be empty.")
        if len(dataset_name.encode("utf-8")) > DATASET_NAME_LIMIT:
            raise ValueError(f"Dataset name length is {len(dataset_name)} which is larger than {DATASET_NAME_LIMIT}")

        # Verify tenant exists
        t = TenantService.get_by_id(db, tenant_id)
        if not t:
            raise ValueError("Tenant not found.")

        # Build payload
        kb_id = get_uuid()
        payload = {
            "id": kb_id,
            "name": dataset_name,
            "tenant_id": tenant_id,
            "created_by": tenant_id,
            "parser_id": (parser_id or "naive"),
            **kwargs
        }

        # Handle embd_id: use tenant default if not provided
        if embd_id is None:
            payload["embd_id"] = t.embd_id
        else:
            payload["embd_id"] = embd_id

        # Parser config must be provided by caller to avoid circular import
        if parser_config is not None:
            payload["parser_config"] = parser_config

        return payload

    @classmethod
    def get_list(cls, db: Session, joined_tenant_ids, user_id, page_number, items_per_page, orderby, desc, id, name):
        """
        # Get list of knowledge bases with filtering and pagination
        # Args:
        #     joined_tenant_ids: List of tenant IDs
        #     user_id: Current user ID
        #     page_number: Page number for pagination
        #     items_per_page: Number of items per page
        #     orderby: Field to order by
        #     desc: Boolean indicating descending order
        #     id: Optional ID filter
        #     name: Optional name filter
        # Returns:
        #     Tuple of (List of knowledge bases, Total count of knowledge bases)
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

        # 获取总数
        total = query.count()

        # 分页查询并返回结果的字典形式和总数
        kbs = query.offset((page_number - 1) * items_per_page).limit(items_per_page).all()
        return [kb.to_dict() for kb in kbs], total

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

    @classmethod
    def atomic_increase_doc_num_by_id(cls, db: Session, kb_id):
        data = {
            "update_time": current_timestamp(),
            "update_date": datetime_format(datetime.now())
        }
        # 在SQLAlchemy中，直接使用表达式来原子递增
        from sqlalchemy import update

        update_stmt = update(cls.model).where(
            cls.model.id == kb_id
        ).values(
            doc_num=cls.model.doc_num + 1,
            **data
        )
        result = db.execute(update_stmt)
        db.commit()
        return result.rowcount > 0

    @classmethod
    def update_document_number_in_init(cls, db: Session, kb_id, doc_num):
        """
        Only use this function when init system

        Args:
            db: 数据库会话对象
            kb_id: 知识库ID
            doc_num: 要设置的文档数量
        """
        # 先检查知识库是否存在
        kb = cls.get_by_id(db, kb_id)
        if not kb:
            return None

        try:
            # 使用SQLAlchemy的update语句直接更新doc_num字段
            # 这样可以避免触发自动更新的时间戳字段

            update_stmt = update(cls.model).where(
                cls.model.id == kb_id
            ).values(
                doc_num=doc_num
            )

            result = db.execute(update_stmt)
            db.commit()

            # 检查是否实际更新了记录
            if result.rowcount == 0:
                # 相当于Peewee中的"no data to save!"情况
                pass  # that's OK

        except Exception as e:
            db.rollback()
            # 如果确实需要处理特定的"no data to save"类似情况
            # 可以在这里添加相应的逻辑
            raise e

    @classmethod
    def decrease_document_num_in_delete(cls, db: Session, kb_id: str, doc_num_info: dict) -> int:
        """删除文档时减少知识库的统计数量（文档数、分块数、token数）"""
        try:
            # 获取知识库记录
            kb_row = cls.get_by_id(db, kb_id)
            if not kb_row:
                raise RuntimeError(f"kb_id {kb_id} does not exist")

            # 构造更新字典
            update_dict = {
                'doc_num': kb_row.doc_num - doc_num_info['doc_num'],
                'chunk_num': kb_row.chunk_num - doc_num_info['chunk_num'],
                'token_num': kb_row.token_num - doc_num_info['token_num'],
                'update_time': current_timestamp(),
                'update_date': datetime_format(datetime.now())
            }

            # 执行更新
            result = db.query(cls.model).filter(
                cls.model.id == kb_id
            ).update(update_dict, synchronize_session=False)
            db.commit()
            return result
        except Exception as e:
            db.rollback()
            raise e