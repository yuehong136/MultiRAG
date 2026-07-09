"""
@project: multirag
@Author：龙
@file： admin_config.py
@date：2025/10/14
@desc: SQLAdmin 配置和初始化
"""

import inspect
import logging
from collections.abc import Callable

from fastapi import FastAPI
from sqladmin import Admin

from api.admin.admin_auth import AdminAuth
from api.admin.admin_models import (
    API4ConversationAdmin,
    # API环境 (3)
    ApiEnvironmentAdmin,
    ApiEnvironmentVariableAdmin,
    APITokenAdmin,
    # 数据分析 (1)
    AskDataHistoryAdmin,
    CanvasTemplateAdmin,
    ConversationAdmin,
    # 对话系统 (4)
    DialogAdmin,
    DocumentAdmin,
    File2DocumentAdmin,
    FileAdmin,
    GlobalApiEnvironmentAdmin,
    GuardDimensionAdmin,
    GuardLabelAdmin,
    GuardLabelLibraryAdmin,
    GuardLibraryAdmin,
    GuardLibraryItemAdmin,
    GuardLogAdmin,
    GuardRuleAdmin,
    # AI护栏 (9)
    GuardServiceAdmin,
    GuardServiceLibraryAdmin,
    # 知识库系统 (5)
    KnowledgebaseAdmin,
    LLMAdmin,
    # LLM管理 (4)
    LLMFactoriesAdmin,
    # MCP服务器 (2)
    MCPServerAdmin,
    # 搜索配置 (1)
    SearchAdmin,
    TaskAdmin,
    TenantAdmin,
    TenantLangfuseAdmin,
    TenantLLMAdmin,
    ToolsDataAdmin,
    # 用户和租户 (3)
    UserAdmin,
    # Canvas画布 (3)
    UserCanvasAdmin,
    UserCanvasVersionAdmin,
    UserTenantAdmin,
    WritingChapterAdmin,
    WritingChapterContentAdmin,
    # 写作助手 (4)
    WritingProjectAdmin,
    WritingReferenceMaterialAdmin,
)
from api.db.db_models import engine

# 总计：37 个模型视图
from common import settings


class _BeartypeTolerantAdmin(Admin):
    """sqladmin 0.28 × beartype 0.22 的兼容垫片。

    beartype 会（有意地，见其 utilcacheobjattr.py 注释）把每个被装饰类的
    ``__sizeof__`` 换成纯 Python 包装函数当作属性藏身处；sqladmin 0.28 的
    ``_find_decorated_funcs`` 对实例全部方法按 ``inspect.getsourcelines``
    排序，unwrap 该包装后落到 C 层 method_descriptor 直接 TypeError。
    这里复刻上游实现、仅把排序键换成可容错版本（拿不到源码行的成员按 -1
    参排——它们本就不是被 @expose/@action 装饰的目标，顺序无影响）。
    sqladmin 后续版本若自带容错可删除本垫片。
    """

    @staticmethod
    def _find_decorated_funcs(
        view: type,
        view_instance: object,
        handle_fn: Callable,
    ) -> None:
        def _source_line_or_default(item: tuple[str, object]) -> int:
            try:
                return inspect.getsourcelines(item[1])[1]  # type: ignore[arg-type]
            except (TypeError, OSError):
                return -1

        funcs = inspect.getmembers(view_instance, predicate=inspect.ismethod)
        for _, func in sorted(funcs, key=_source_line_or_default, reverse=True):
            handle_fn(func, view, view_instance)


def setup_admin(app: FastAPI) -> Admin:
    """
    配置并初始化SQLAdmin管理后台

    Args:
        app: FastAPI应用实例

    Returns:
        Admin: SQLAdmin实例
    """
    # 创建认证后端，使用项目统一的SECRET_KEY配置
    authentication_backend = AdminAuth(secret_key=settings.SECRET_KEY)

    # 创建Admin实例
    admin = _BeartypeTolerantAdmin(
        app=app,
        engine=engine,
        authentication_backend=authentication_backend,
        title="MultiRAG 管理后台",
        base_url="/admin",
    )

    # ========== 注册所有模型视图 ==========

    # 用户和租户管理
    admin.add_view(UserAdmin)
    admin.add_view(TenantAdmin)
    admin.add_view(UserTenantAdmin)

    # LLM管理
    admin.add_view(LLMFactoriesAdmin)
    admin.add_view(LLMAdmin)
    admin.add_view(TenantLLMAdmin)
    admin.add_view(TenantLangfuseAdmin)

    # AI护栏管理
    admin.add_view(GuardServiceAdmin)
    admin.add_view(GuardServiceLibraryAdmin)
    admin.add_view(GuardRuleAdmin)
    admin.add_view(GuardLogAdmin)
    admin.add_view(GuardLibraryItemAdmin)
    admin.add_view(GuardLibraryAdmin)
    admin.add_view(GuardLabelAdmin)
    admin.add_view(GuardLabelLibraryAdmin)
    admin.add_view(GuardDimensionAdmin)

    # 知识库系统
    admin.add_view(KnowledgebaseAdmin)
    admin.add_view(DocumentAdmin)
    admin.add_view(FileAdmin)
    admin.add_view(File2DocumentAdmin)
    admin.add_view(TaskAdmin)

    # 对话系统
    admin.add_view(DialogAdmin)
    admin.add_view(ConversationAdmin)
    admin.add_view(APITokenAdmin)
    admin.add_view(API4ConversationAdmin)

    # Canvas画布系统
    admin.add_view(UserCanvasAdmin)
    admin.add_view(CanvasTemplateAdmin)
    admin.add_view(UserCanvasVersionAdmin)

    # 写作助手系统
    admin.add_view(WritingProjectAdmin)
    admin.add_view(WritingChapterAdmin)
    admin.add_view(WritingReferenceMaterialAdmin)
    admin.add_view(WritingChapterContentAdmin)

    # 数据分析系统
    admin.add_view(AskDataHistoryAdmin)

    # API环境管理
    admin.add_view(ApiEnvironmentAdmin)
    admin.add_view(ApiEnvironmentVariableAdmin)
    admin.add_view(GlobalApiEnvironmentAdmin)

    # MCP服务器管理
    admin.add_view(MCPServerAdmin)
    admin.add_view(ToolsDataAdmin)

    # 搜索配置
    admin.add_view(SearchAdmin)

    logging.info("SQLAdmin has been initialized successfully")
    logging.info("Admin panel available at: /admin (共注册 37 个模型视图)")

    return admin
