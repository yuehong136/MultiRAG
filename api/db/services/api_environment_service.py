"""
@project: multirag
@Author：龙
@file： environment_service.py
@date：2025/1/15 10:00
@desc: 环境管理服务核心实现
"""

import logging
import re
from datetime import datetime

from sqlalchemy import and_, desc, func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from api.db.db_models import ApiEnvironment, ApiEnvironmentVariable, GlobalApiEnvironment
from common.misc_utils import get_uuid

from .api_environment_models import (
    BatchVariablesRequest,
    EnvironmentCreate,
    EnvironmentDetailResponse,
    EnvironmentDuplicateRequest,
    EnvironmentListResponse,
    EnvironmentQueryParams,
    EnvironmentUpdate,
    EnvironmentVariableCreate,
    EnvironmentVariableResponse,
    EnvironmentVariableUpdate,
    GlobalEnvironmentResponse,
    PaginatedEnvironmentResponse,
    VariableResolveResponse,
)

logger = logging.getLogger(__name__)


class ApiEnvironmentService:
    """环境管理服务核心类"""

    def __init__(self):
        """初始化环境服务"""
        self.variable_pattern = re.compile(r"\{\{(\w+)\}\}")

    def get_environments(self, db: Session, tenant_id: str, params: EnvironmentQueryParams) -> PaginatedEnvironmentResponse:
        """
        获取用户的环境列表

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            params: 查询参数

        Returns:
            分页环境列表
        """
        try:
            # 构建查询
            query = db.query(ApiEnvironment).filter(ApiEnvironment.tenant_id == tenant_id)

            # 搜索过滤
            if params.search:
                search_term = f"%{params.search}%"
                query = query.filter(or_(ApiEnvironment.name.ilike(search_term), ApiEnvironment.description.ilike(search_term)))

            # 默认环境过滤
            if params.is_default is not None:
                query = query.filter(ApiEnvironment.is_default == params.is_default)

            # 总数统计
            total = query.count()

            # 分页和排序
            environments = query.order_by(desc(ApiEnvironment.is_default), desc(ApiEnvironment.create_date)).offset((params.page - 1) * params.page_size).limit(params.page_size).all()

            # 统计每个环境的变量数量
            env_ids = [env.id for env in environments]
            if env_ids:
                var_counts = (
                    db.query(ApiEnvironmentVariable.environment_id, func.count(ApiEnvironmentVariable.id).label("count"))
                    .filter(ApiEnvironmentVariable.environment_id.in_(env_ids))
                    .group_by(ApiEnvironmentVariable.environment_id)
                    .all()
                )

                var_count_map = {vc.environment_id: vc.count for vc in var_counts}
            else:
                var_count_map = {}

            # 构建响应
            items = []
            for env in environments:
                env_dict = env.to_dict()
                env_dict["variables_count"] = var_count_map.get(env.id, 0)
                env_data = EnvironmentListResponse.model_validate(env_dict)
                items.append(env_data)

            total_pages = (total + params.page_size - 1) // params.page_size

            return PaginatedEnvironmentResponse(items=items, total=total, page=params.page, page_size=params.page_size, total_pages=total_pages)

        except SQLAlchemyError as e:
            logger.error(f"获取环境列表失败: {e!s}")
            raise Exception(f"获取环境列表失败: {e!s}")

    def get_environment_detail(self, db: Session, tenant_id: str, environment_id: str) -> EnvironmentDetailResponse:
        """
        获取环境详情

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            environment_id: 环境ID

        Returns:
            环境详情
        """
        try:
            # 查询环境
            environment = db.query(ApiEnvironment).filter(and_(ApiEnvironment.id == environment_id, ApiEnvironment.tenant_id == tenant_id)).first()

            if not environment:
                raise Exception("环境不存在")

            # 查询环境变量
            variables = db.query(ApiEnvironmentVariable).filter(ApiEnvironmentVariable.environment_id == environment_id).order_by(ApiEnvironmentVariable.key_name).all()

            # 构建响应
            env_dict = environment.to_dict()
            env_dict["variables"] = [EnvironmentVariableResponse.model_validate(var.to_dict()) for var in variables]
            env_data = EnvironmentDetailResponse.model_validate(env_dict)

            return env_data

        except SQLAlchemyError as e:
            logger.error(f"获取环境详情失败: {e!s}")
            raise Exception(f"获取环境详情失败: {e!s}")

    def create_environment(self, db: Session, tenant_id: str, env_data: EnvironmentCreate) -> EnvironmentDetailResponse:
        """
        创建环境

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            env_data: 环境数据

        Returns:
            创建的环境详情
        """
        try:
            # 检查环境名称是否已存在
            existing = db.query(ApiEnvironment).filter(and_(ApiEnvironment.tenant_id == tenant_id, ApiEnvironment.name == env_data.name)).first()

            if existing:
                raise Exception("环境名称已存在")

            # 如果设为默认环境，需要先取消其他默认环境
            if env_data.is_default:
                db.query(ApiEnvironment).filter(and_(ApiEnvironment.tenant_id == tenant_id, ApiEnvironment.is_default == True)).update({"is_default": False})

            # 创建环境
            environment = ApiEnvironment(id=get_uuid(), tenant_id=tenant_id, name=env_data.name, description=env_data.description, is_default=env_data.is_default, is_global=env_data.is_global)

            db.add(environment)
            db.flush()  # 获取ID

            # 创建环境变量
            variables = []
            for var_data in env_data.variables:
                # 检查变量名是否重复
                existing_var = db.query(ApiEnvironmentVariable).filter(and_(ApiEnvironmentVariable.environment_id == environment.id, ApiEnvironmentVariable.key_name == var_data.key_name)).first()

                if existing_var:
                    raise Exception(f"变量名 '{var_data.key_name}' 已存在")

                variable = ApiEnvironmentVariable(
                    id=get_uuid(),
                    environment_id=environment.id,
                    key_name=var_data.key_name,
                    key_value=var_data.key_value,
                    description=var_data.description,
                    is_secret=var_data.is_secret,
                    variable_type=var_data.variable_type.value,
                )

                db.add(variable)
                variables.append(variable)

            db.commit()

            # 构建响应
            env_dict = environment.to_dict()
            env_dict["variables"] = [EnvironmentVariableResponse.model_validate(var.to_dict()) for var in variables]
            env_response = EnvironmentDetailResponse.model_validate(env_dict)

            return env_response

        except IntegrityError as e:
            db.rollback()
            logger.error(f"创建环境失败(完整性约束): {e!s}")
            raise Exception("环境名称已存在")
        except SQLAlchemyError as e:
            db.rollback()
            logger.error(f"创建环境失败: {e!s}")
            raise Exception(f"创建环境失败: {e!s}")

    def update_environment(self, db: Session, tenant_id: str, environment_id: str, env_data: EnvironmentUpdate) -> EnvironmentDetailResponse:
        """
        更新环境

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            environment_id: 环境ID
            env_data: 更新数据

        Returns:
            更新后的环境详情
        """
        try:
            # 查询环境
            environment = db.query(ApiEnvironment).filter(and_(ApiEnvironment.id == environment_id, ApiEnvironment.tenant_id == tenant_id)).first()

            if not environment:
                raise Exception("环境不存在")

            # 检查环境名称是否已存在（排除当前环境）
            if env_data.name and env_data.name != environment.name:
                existing = db.query(ApiEnvironment).filter(and_(ApiEnvironment.tenant_id == tenant_id, ApiEnvironment.name == env_data.name, ApiEnvironment.id != environment_id)).first()

                if existing:
                    raise Exception("环境名称已存在")

            # 如果设为默认环境，需要先取消其他默认环境
            if env_data.is_default:
                db.query(ApiEnvironment).filter(and_(ApiEnvironment.tenant_id == tenant_id, ApiEnvironment.is_default == True, ApiEnvironment.id != environment_id)).update({"is_default": False})

            # 更新环境
            update_data = env_data.model_dump(exclude_unset=True)
            if update_data:
                update_data["update_date"] = datetime.now()
                db.query(ApiEnvironment).filter(ApiEnvironment.id == environment_id).update(update_data)

            db.commit()

            # 返回更新后的环境详情
            return self.get_environment_detail(db, tenant_id, environment_id)

        except IntegrityError as e:
            db.rollback()
            logger.error(f"更新环境失败(完整性约束): {e!s}")
            raise Exception("环境名称已存在")
        except SQLAlchemyError as e:
            db.rollback()
            logger.error(f"更新环境失败: {e!s}")
            raise Exception(f"更新环境失败: {e!s}")

    def delete_environment(self, db: Session, tenant_id: str, environment_id: str) -> bool:
        """
        删除环境

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            environment_id: 环境ID

        Returns:
            是否删除成功
        """
        try:
            # 查询环境
            environment = db.query(ApiEnvironment).filter(and_(ApiEnvironment.id == environment_id, ApiEnvironment.tenant_id == tenant_id)).first()

            if not environment:
                raise Exception("环境不存在")

            # 先查询要删除的环境变量数量
            variables_count = db.query(ApiEnvironmentVariable).filter(ApiEnvironmentVariable.environment_id == environment_id).count()

            logger.info(f"准备删除环境 {environment_id}，包含 {variables_count} 个环境变量")

            # 删除环境变量
            deleted_variables = db.query(ApiEnvironmentVariable).filter(ApiEnvironmentVariable.environment_id == environment_id).delete(synchronize_session=False)

            logger.info(f"成功删除 {deleted_variables} 个环境变量")

            # 删除环境
            deleted_environments = db.query(ApiEnvironment).filter(ApiEnvironment.id == environment_id).delete(synchronize_session=False)

            if deleted_environments == 0:
                raise Exception("环境删除失败，可能已被其他操作删除")

            logger.info(f"成功删除环境 {environment_id}")

            db.commit()
            return True

        except SQLAlchemyError as e:
            db.rollback()
            logger.error(f"删除环境失败: {e!s}")
            raise Exception(f"删除环境失败: {e!s}")

    def duplicate_environment(self, db: Session, tenant_id: str, environment_id: str, duplicate_data: EnvironmentDuplicateRequest) -> EnvironmentDetailResponse:
        """
        复制环境

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            environment_id: 环境ID
            duplicate_data: 复制请求数据

        Returns:
            复制的环境详情
        """
        try:
            # 查询原环境
            original_env = db.query(ApiEnvironment).filter(and_(ApiEnvironment.id == environment_id, ApiEnvironment.tenant_id == tenant_id)).first()

            if not original_env:
                raise Exception("原环境不存在")

            # 检查新环境名称是否已存在
            existing = db.query(ApiEnvironment).filter(and_(ApiEnvironment.tenant_id == tenant_id, ApiEnvironment.name == duplicate_data.new_name)).first()

            if existing:
                raise Exception("环境名称已存在")

            # 创建新环境
            new_env = ApiEnvironment(
                id=get_uuid(),
                tenant_id=tenant_id,
                name=duplicate_data.new_name,
                description=original_env.description,
                is_default=False,  # 复制的环境不设为默认
                is_global=original_env.is_global,
            )

            db.add(new_env)
            db.flush()

            # 复制环境变量
            original_vars = db.query(ApiEnvironmentVariable).filter(ApiEnvironmentVariable.environment_id == environment_id).all()

            new_vars = []
            for var in original_vars:
                new_var = ApiEnvironmentVariable(
                    id=get_uuid(), environment_id=new_env.id, key_name=var.key_name, key_value=var.key_value, description=var.description, is_secret=var.is_secret, variable_type=var.variable_type
                )

                db.add(new_var)
                new_vars.append(new_var)

            db.commit()

            # 构建响应
            env_dict = new_env.to_dict()
            env_dict["variables"] = [EnvironmentVariableResponse.model_validate(var.to_dict()) for var in new_vars]
            env_response = EnvironmentDetailResponse.model_validate(env_dict)

            return env_response

        except IntegrityError as e:
            db.rollback()
            logger.error(f"复制环境失败(完整性约束): {e!s}")
            raise Exception("环境名称已存在")
        except SQLAlchemyError as e:
            db.rollback()
            logger.error(f"复制环境失败: {e!s}")
            raise Exception(f"复制环境失败: {e!s}")

    def set_default_environment(self, db: Session, tenant_id: str, environment_id: str) -> EnvironmentDetailResponse:
        """
        设置默认环境

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            environment_id: 环境ID

        Returns:
            设置后的环境详情
        """
        try:
            # 查询环境
            environment = db.query(ApiEnvironment).filter(and_(ApiEnvironment.id == environment_id, ApiEnvironment.tenant_id == tenant_id)).first()

            if not environment:
                raise Exception("环境不存在")

            # 取消其他默认环境
            db.query(ApiEnvironment).filter(and_(ApiEnvironment.tenant_id == tenant_id, ApiEnvironment.is_default == True)).update({"is_default": False})

            # 设置当前环境为默认
            db.query(ApiEnvironment).filter(ApiEnvironment.id == environment_id).update({"is_default": True, "update_date": datetime.now()})

            db.commit()

            # 返回环境详情
            return self.get_environment_detail(db, tenant_id, environment_id)

        except SQLAlchemyError as e:
            db.rollback()
            logger.error(f"设置默认环境失败: {e!s}")
            raise Exception(f"设置默认环境失败: {e!s}")

    def create_variable(self, db: Session, tenant_id: str, environment_id: str, var_data: EnvironmentVariableCreate) -> EnvironmentVariableResponse:
        """
        创建环境变量

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            environment_id: 环境ID
            var_data: 变量数据

        Returns:
            创建的变量
        """
        try:
            # 验证环境是否存在且属于用户
            environment = db.query(ApiEnvironment).filter(and_(ApiEnvironment.id == environment_id, ApiEnvironment.tenant_id == tenant_id)).first()

            if not environment:
                raise Exception("环境不存在")

            # 检查变量名是否已存在
            existing = db.query(ApiEnvironmentVariable).filter(and_(ApiEnvironmentVariable.environment_id == environment_id, ApiEnvironmentVariable.key_name == var_data.key_name)).first()

            if existing:
                raise Exception("变量名已存在")

            # 创建变量
            variable = ApiEnvironmentVariable(
                id=get_uuid(),
                environment_id=environment_id,
                key_name=var_data.key_name,
                key_value=var_data.key_value,
                description=var_data.description,
                is_secret=var_data.is_secret,
                variable_type=var_data.variable_type.value,
            )

            db.add(variable)
            db.commit()

            return EnvironmentVariableResponse.model_validate(variable.to_dict())

        except IntegrityError as e:
            db.rollback()
            logger.error(f"创建变量失败(完整性约束): {e!s}")
            raise Exception("变量名已存在")
        except SQLAlchemyError as e:
            db.rollback()
            logger.error(f"创建变量失败: {e!s}")
            raise Exception(f"创建变量失败: {e!s}")

    def update_variable(self, db: Session, tenant_id: str, environment_id: str, variable_id: str, var_data: EnvironmentVariableUpdate) -> EnvironmentVariableResponse:
        """
        更新环境变量

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            environment_id: 环境ID
            variable_id: 变量ID
            var_data: 更新数据

        Returns:
            更新后的变量
        """
        try:
            # 验证环境是否存在且属于用户
            environment = db.query(ApiEnvironment).filter(and_(ApiEnvironment.id == environment_id, ApiEnvironment.tenant_id == tenant_id)).first()

            if not environment:
                raise Exception("环境不存在")

            # 查询变量
            variable = db.query(ApiEnvironmentVariable).filter(and_(ApiEnvironmentVariable.id == variable_id, ApiEnvironmentVariable.environment_id == environment_id)).first()

            if not variable:
                raise Exception("变量不存在")

            # 检查变量名是否已存在（排除当前变量）
            if var_data.key_name and var_data.key_name != variable.key_name:
                existing = (
                    db.query(ApiEnvironmentVariable)
                    .filter(and_(ApiEnvironmentVariable.environment_id == environment_id, ApiEnvironmentVariable.key_name == var_data.key_name, ApiEnvironmentVariable.id != variable_id))
                    .first()
                )

                if existing:
                    raise Exception("变量名已存在")

            # 更新变量
            update_data = var_data.model_dump(exclude_unset=True)
            if update_data:
                if "variable_type" in update_data:
                    update_data["variable_type"] = update_data["variable_type"].value
                update_data["update_date"] = datetime.now()
                db.query(ApiEnvironmentVariable).filter(ApiEnvironmentVariable.id == variable_id).update(update_data)

            db.commit()

            # 重新查询变量
            updated_var = db.query(ApiEnvironmentVariable).filter(ApiEnvironmentVariable.id == variable_id).first()
            return EnvironmentVariableResponse.model_validate(updated_var.to_dict())

        except IntegrityError as e:
            db.rollback()
            logger.error(f"更新变量失败(完整性约束): {e!s}")
            raise Exception("变量名已存在")
        except SQLAlchemyError as e:
            db.rollback()
            logger.error(f"更新变量失败: {e!s}")
            raise Exception(f"更新变量失败: {e!s}")

    def delete_variable(self, db: Session, tenant_id: str, environment_id: str, variable_id: str) -> bool:
        """
        删除环境变量

        Args:
            db: 数据库会话
            tenant_id: 用户ID
            environment_id: 环境ID
            variable_id: 变量ID

        Returns:
            是否删除成功
        """
        try:
            # 验证环境是否存在且属于用户
            environment = db.query(ApiEnvironment).filter(and_(ApiEnvironment.id == environment_id, ApiEnvironment.tenant_id == tenant_id)).first()

            if not environment:
                raise Exception("环境不存在")

            # 删除变量
            deleted_count = db.query(ApiEnvironmentVariable).filter(and_(ApiEnvironmentVariable.id == variable_id, ApiEnvironmentVariable.environment_id == environment_id)).delete()

            if deleted_count == 0:
                raise Exception("变量不存在")

            db.commit()
            return True

        except SQLAlchemyError as e:
            db.rollback()
            logger.error(f"删除变量失败: {e!s}")
            raise Exception(f"删除变量失败: {e!s}")

    def batch_update_variables(self, db: Session, tenant_id: str, environment_id: str, batch_data: BatchVariablesRequest) -> list[EnvironmentVariableResponse]:
        """
        批量更新环境变量

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            environment_id: 环境ID
            batch_data: 批量数据

        Returns:
            更新后的变量列表
        """
        try:
            # 验证环境是否存在且属于用户
            environment = db.query(ApiEnvironment).filter(and_(ApiEnvironment.id == environment_id, ApiEnvironment.tenant_id == tenant_id)).first()

            if not environment:
                raise Exception("环境不存在")

            # 删除现有变量
            db.query(ApiEnvironmentVariable).filter(ApiEnvironmentVariable.environment_id == environment_id).delete()

            # 检查变量名重复
            var_names = [var.key_name for var in batch_data.variables]
            if len(var_names) != len(set(var_names)):
                raise Exception("变量名不能重复")

            # 创建新变量
            new_variables = []
            for var_data in batch_data.variables:
                variable = ApiEnvironmentVariable(
                    id=get_uuid(),
                    environment_id=environment_id,
                    key_name=var_data.key_name,
                    key_value=var_data.key_value,
                    description=var_data.description,
                    is_secret=var_data.is_secret,
                    variable_type=var_data.variable_type.value,
                )

                db.add(variable)
                new_variables.append(variable)

            db.commit()

            return [EnvironmentVariableResponse.model_validate(var.to_dict()) for var in new_variables]

        except SQLAlchemyError as e:
            db.rollback()
            logger.error(f"批量更新变量失败: {e!s}")
            raise Exception(f"批量更新变量失败: {e!s}")

    def resolve_variables(self, db: Session, tenant_id: str, environment_id: str, text: str) -> VariableResolveResponse:
        """
        解析变量

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            environment_id: 环境ID
            text: 要解析的文本

        Returns:
            解析结果
        """
        try:
            # 验证环境是否存在且属于用户
            environment = db.query(ApiEnvironment).filter(and_(ApiEnvironment.id == environment_id, ApiEnvironment.tenant_id == tenant_id)).first()

            if not environment:
                raise Exception("环境不存在")

            # 获取环境变量
            variables = db.query(ApiEnvironmentVariable).filter(ApiEnvironmentVariable.environment_id == environment_id).all()

            var_map = {var.key_name: var.key_value for var in variables}

            # 查找文本中的变量
            found_vars = self.variable_pattern.findall(text)
            variables_used = []
            missing_variables = []

            # 替换变量
            resolved_text = text
            for var_name in found_vars:
                if var_name in var_map:
                    resolved_text = resolved_text.replace(f"{{{{{var_name}}}}}", var_map[var_name])
                    if var_name not in variables_used:
                        variables_used.append(var_name)
                else:
                    if var_name not in missing_variables:
                        missing_variables.append(var_name)

            return VariableResolveResponse(resolved_text=resolved_text, variables_used=variables_used, missing_variables=missing_variables)

        except SQLAlchemyError as e:
            logger.error(f"解析变量失败: {e!s}")
            raise Exception(f"解析变量失败: {e!s}")

    def get_global_environments(self, db: Session) -> list[GlobalEnvironmentResponse]:
        """
        获取全局预设环境

        Args:
            db: 数据库会话

        Returns:
            全局环境列表
        """
        try:
            global_envs = db.query(GlobalApiEnvironment).filter(GlobalApiEnvironment.is_active == True).order_by(GlobalApiEnvironment.name).all()

            return [GlobalEnvironmentResponse.model_validate(env.to_dict()) for env in global_envs]

        except SQLAlchemyError as e:
            logger.error(f"获取全局环境失败: {e!s}")
            raise Exception(f"获取全局环境失败: {e!s}")


# 创建服务实例
environment_service = ApiEnvironmentService()
