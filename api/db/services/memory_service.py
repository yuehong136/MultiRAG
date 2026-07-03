import logging

from sqlalchemy import desc, select, update
from sqlalchemy.orm import Session

from api.constants import MEMORY_NAME_LIMIT
from api.db.db_models import Memory, User
from api.db.services import duplicate_name
from api.db.services.common_service import CommonService
from api.utils.memory_utils import calculate_memory_type
from common.misc_utils import get_uuid
from memory.utils.prompt_util import PromptAssembler

logger = logging.getLogger(__name__)


class MemoryService(CommonService):
    """Memory服务层类

    提供Memory的CRUD操作，采用SQLAlchemy 2.0风格
    """
    model = Memory

    @classmethod
    def get_by_memory_id(cls, db: Session, memory_id: str) -> Memory | None:
        """
        根据Memory ID获取Memory对象

        SQLAlchemy 2.0 推荐：使用 session.get() 进行主键查询
        - 会先检查 Session 的 identity map，如果对象已加载则直接返回
        - 代码更简洁，语义更清晰

        Args:
            db: 数据库会话
            memory_id: Memory ID

        Returns:
            Memory对象，未找到返回None
        """
        return db.get(cls.model, memory_id)

    @classmethod
    def get_by_tenant_id(cls, db: Session, tenant_id: str) -> list[Memory]:
        """
        根据租户ID获取所有Memory

        Args:
            db: 数据库会话
            tenant_id: 租户ID

        Returns:
            Memory列表
        """
        stmt = select(cls.model).where(cls.model.tenant_id == tenant_id)
        return list(db.scalars(stmt).all())

    @classmethod
    def get_all_memory(cls, db: Session) -> list[Memory]:
        """
        获取所有Memory

        Args:
            db: 数据库会话

        Returns:
            Memory列表
        """
        stmt = select(cls.model)
        return list(db.scalars(stmt).all())

    @classmethod
    def get_with_owner_name_by_id(cls, db: Session, memory_id: str):
        """
        根据Memory ID获取Memory详情，包含所有者名称

        Args:
            db: 数据库会话
            memory_id: Memory ID

        Returns:
            包含owner_name的Memory Row对象，未找到返回None
        """
        stmt = (
            select(
                cls.model.id,
                cls.model.name,
                cls.model.avatar,
                cls.model.tenant_id,
                User.nickname.label("owner_name"),
                cls.model.memory_type,
                cls.model.storage_type,
                cls.model.embd_id,
                cls.model.tenant_embd_id,
                cls.model.llm_id,
                cls.model.tenant_llm_id,
                cls.model.permissions,
                cls.model.description,
                cls.model.memory_size,
                cls.model.forgetting_policy,
                cls.model.temperature,
                cls.model.system_prompt,
                cls.model.user_prompt,
                cls.model.create_date,
                cls.model.create_time
            )
            .join(User, cls.model.tenant_id == User.id)
            .where(cls.model.id == memory_id)
        )
        return db.execute(stmt).one_or_none()

    @classmethod
    def get_by_filter(
        cls,
        db: Session,
        filter_dict: dict,
        keywords: str = "",
        page: int = 1,
        page_size: int = 50
    ) -> tuple[list[dict], int]:
        """
        根据过滤条件获取Memory列表

        Args:
            db: 数据库会话
            filter_dict: 过滤条件字典，支持tenant_id, memory_type, storage_type
            keywords: 名称搜索关键词
            page: 页码
            page_size: 每页数量

        Returns:
            tuple: (Memory列表, 总数)
        """
        # 构建查询
        stmt = (
            select(
                cls.model.id,
                cls.model.name,
                cls.model.avatar,
                cls.model.tenant_id,
                User.nickname.label("owner_name"),
                cls.model.memory_type,
                cls.model.storage_type,
                cls.model.embd_id,
                cls.model.tenant_embd_id,
                cls.model.llm_id,
                cls.model.tenant_llm_id,
                cls.model.permissions,
                cls.model.description,
                cls.model.create_time,
                cls.model.create_date
            )
            .join(User, cls.model.tenant_id == User.id)
        )

        # 应用过滤条件
        if filter_dict.get("tenant_id"):
            tenant_ids = filter_dict["tenant_id"]
            if isinstance(tenant_ids, list):
                stmt = stmt.where(cls.model.tenant_id.in_(tenant_ids))
            else:
                stmt = stmt.where(cls.model.tenant_id == tenant_ids)

        if filter_dict.get("memory_type"):
            memory_type_int = calculate_memory_type(filter_dict["memory_type"])
            # 使用位与运算匹配
            stmt = stmt.where(cls.model.memory_type.bitwise_and(memory_type_int) > 0)

        if filter_dict.get("storage_type"):
            stmt = stmt.where(cls.model.storage_type == filter_dict["storage_type"])

        if keywords:
            stmt = stmt.where(cls.model.name.contains(keywords))

        # 获取总数（使用子查询）
        count_stmt = select(cls.model.id)
        if filter_dict.get("tenant_id"):
            tenant_ids = filter_dict["tenant_id"]
            if isinstance(tenant_ids, list):
                count_stmt = count_stmt.where(cls.model.tenant_id.in_(tenant_ids))
            else:
                count_stmt = count_stmt.where(cls.model.tenant_id == tenant_ids)
        if filter_dict.get("memory_type"):
            memory_type_int = calculate_memory_type(filter_dict["memory_type"])
            count_stmt = count_stmt.where(cls.model.memory_type.bitwise_and(memory_type_int) > 0)
        if filter_dict.get("storage_type"):
            count_stmt = count_stmt.where(cls.model.storage_type == filter_dict["storage_type"])
        if keywords:
            count_stmt = count_stmt.where(cls.model.name.contains(keywords))

        count = len(db.scalars(count_stmt).all())

        # 排序和分页
        stmt = stmt.order_by(desc(cls.model.update_time))
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)

        # 执行查询
        results = db.execute(stmt).all()
        memory_list = [dict(row._mapping) for row in results]

        return memory_list, count

    @classmethod
    def create_memory(
        cls,
        db: Session,
        tenant_id: str,
        name: str,
        memory_type: list[str],
        embd_id: str,
        llm_id: str,
        tenant_embd_id: int | None = None,
        tenant_llm_id: int | None = None,
    ) -> tuple[bool, Memory | str]:
        """
        创建新的Memory

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            name: Memory名称
            memory_type: Memory类型列表，如["raw", "semantic"]
            embd_id: 嵌入模型ID
            llm_id: LLM模型ID

        Returns:
            tuple: (是否成功, Memory对象或错误信息)
        """
        # 检查名称重复
        def name_exists(name: str, tenant_id: str) -> bool:
            stmt = select(cls.model.id).where(
                cls.model.name == name,
                cls.model.tenant_id == tenant_id
            )
            return db.scalars(stmt).first() is not None

        # 生成唯一名称
        memory_name = duplicate_name(
            lambda **kw: name_exists(kw["name"], kw["tenant_id"]),
            name=name,
            tenant_id=tenant_id
        )

        if len(memory_name) > MEMORY_NAME_LIMIT:
            return False, f"Memory name '{memory_name}' exceeds limit of {MEMORY_NAME_LIMIT}."

        # 创建Memory记录
        memory_id = get_uuid()
        memory_type_int = calculate_memory_type(memory_type)

        try:
            # 根据 memory_type 生成默认的 system_prompt
            system_prompt = PromptAssembler.assemble_system_prompt({"memory_type": memory_type})

            memory = cls.model(
                id=memory_id,
                name=memory_name,
                memory_type=memory_type_int,
                tenant_id=tenant_id,
                embd_id=embd_id,
                tenant_embd_id=tenant_embd_id,
                llm_id=llm_id,
                tenant_llm_id=tenant_llm_id,
                system_prompt=system_prompt,
            )
            db.add(memory)
            db.commit()
            db.refresh(memory)
            return True, memory
        except Exception as e:
            db.rollback()
            logger.error(f"创建Memory失败: {e}")
            return False, str(e)

    @classmethod
    def update_memory(cls, db: Session, tenant_id: str, memory_id: str, update_dict: dict) -> int:
        """
        更新Memory

        Args:
            db: 数据库会话
            memory_id: Memory ID
            update_dict: 更新字段字典

        Returns:
            更新的记录数
        """
        if not update_dict:
            return 0

        # 处理temperature字段类型转换
        if "temperature" in update_dict and isinstance(update_dict["temperature"], str):
            update_dict["temperature"] = float(update_dict["temperature"])

        # 处理memory_type字段类型转换
        if "memory_type" in update_dict and isinstance(update_dict["memory_type"], list):
            update_dict["memory_type"] = calculate_memory_type(update_dict["memory_type"])

        # 处理名称重复
        if "name" in update_dict:
            def name_exists(name: str, tenant_id: str) -> bool:
                stmt = select(cls.model.id).where(
                    cls.model.name == name,
                    cls.model.tenant_id == tenant_id,
                    cls.model.id != memory_id  # 排除自身
                )
                return db.scalars(stmt).first() is not None

            update_dict["name"] = duplicate_name(
                lambda **kw: name_exists(kw["name"], kw["tenant_id"]),
                name=update_dict["name"],
                tenant_id=tenant_id
            )

        # 更新时间由BaseModel的before_update事件处理
        stmt = (
            update(cls.model)
            .where(cls.model.id == memory_id)
            .values(**update_dict)
        )
        result = db.execute(stmt)
        db.commit()
        return result.rowcount

    @classmethod
    def delete_memory(cls, db: Session, memory_id: str) -> int:
        """
        删除Memory

        Args:
            db: 数据库会话
            memory_id: Memory ID

        Returns:
            删除的记录数
        """
        return cls.delete_by_id(db, memory_id)

    @classmethod
    def get_null_tenant_embd_id_row(cls, db: Session):
        stmt = select(cls.model.id, cls.model.tenant_id, cls.model.embd_id).where(cls.model.tenant_embd_id.is_(None))
        return db.execute(stmt).all()

    @classmethod
    def get_null_tenant_llm_id_row(cls, db: Session):
        stmt = select(cls.model.id, cls.model.tenant_id, cls.model.llm_id).where(cls.model.tenant_llm_id.is_(None))
        return db.execute(stmt).all()
