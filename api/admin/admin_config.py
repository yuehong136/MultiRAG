# coding=utf-8
"""
@project: multirag
@Author：龙
@file： admin_config.py
@date：2025/10/14
@desc: SQLAdmin 配置和初始化
"""
import logging
from sqladmin import Admin
from fastapi import FastAPI

from api.db.db_models import engine
from api.admin.admin_auth import AdminAuth
from api.admin.admin_models import (
    # 用户和租户 (3)
    UserAdmin, TenantAdmin, UserTenantAdmin,
    # LLM管理 (4)
    LLMFactoriesAdmin, LLMAdmin, TenantLLMAdmin, TenantLangfuseAdmin,
    # AI护栏 (9)
    GuardServiceAdmin, GuardServiceLibraryAdmin, GuardRuleAdmin, GuardLogAdmin,
    GuardLibraryItemAdmin, GuardLibraryAdmin, GuardLabelAdmin, GuardLabelLibraryAdmin, GuardDimensionAdmin,
    # 知识库系统 (5)
    KnowledgebaseAdmin, DocumentAdmin, FileAdmin, File2DocumentAdmin, TaskAdmin,
    # 对话系统 (4)
    DialogAdmin, ConversationAdmin, APITokenAdmin, API4ConversationAdmin,
    # Canvas画布 (3)
    UserCanvasAdmin, CanvasTemplateAdmin, UserCanvasVersionAdmin,
    # 写作助手 (4)
    WritingProjectAdmin, WritingChapterAdmin, WritingReferenceMaterialAdmin, WritingChapterContentAdmin,
    # 数据分析 (1)
    AskDataHistoryAdmin,
    # API环境 (3)
    ApiEnvironmentAdmin, ApiEnvironmentVariableAdmin, GlobalApiEnvironmentAdmin,
    # MCP服务器 (2)
    MCPServerAdmin, ToolsDataAdmin,
    # 搜索配置 (1)
    SearchAdmin,
)
# 总计：37 个模型视图
from common import settings


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
    admin = Admin(
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
    logging.info(f"Admin panel available at: /admin (共注册 37 个模型视图)")
    
    return admin
