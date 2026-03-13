# coding=utf-8
"""
@project: multirag
@Author：龙
@file： admin_models.py
@date：2024/10/14
@desc: SQLAdmin 管理后台模型定义 - 完整版(37个模型，所有字段)
"""
from sqladmin import ModelView
from api.db.db_models import (
    # 用户和租户
    User, Tenant, UserTenant,
    # LLM管理
    LLMFactories, LLM, TenantLLM, TenantLangfuse,
    # AI护栏
    GuardService, GuardServiceLibrary, GuardRule, GuardLog,
    GuardLibraryItem, GuardLibrary, GuardLabel, GuardLabelLibrary, GuardDimension,
    # 知识库系统
    Knowledgebase, Document, File, File2Document, Task,
    # 对话系统
    Dialog, Conversation, APIToken, API4Conversation,
    # Canvas画布
    UserCanvas, CanvasTemplate, UserCanvasVersion,
    # 写作助手
    WritingProject, WritingChapter, WritingReferenceMaterial, WritingChapterContent,
    # 数据分析
    AskDataHistory,
    # API环境
    ApiEnvironment, ApiEnvironmentVariable, GlobalApiEnvironment,
    # MCP服务器
    MCPServer, ToolsData,
    # 搜索配置
    Search,
)


# ========== 用户和租户管理 ==========

class UserAdmin(ModelView, model=User):
    """用户管理"""
    name = "用户"
    name_plural = "用户管理"
    icon = "fa-solid fa-user"
    
    column_list = [User.id, User.nickname, User.email, User.language, User.color_schema,
                   User.timezone, User.last_login_time, User.is_authenticated, User.is_active,
                   User.is_anonymous, User.login_channel, User.status, User.is_superuser,
                   User.create_date, User.update_date]
    column_searchable_list = [User.nickname, User.email]
    column_sortable_list = [User.create_date, User.last_login_time]
    form_excluded_columns = [User.password, User.access_token]
    column_default_sort = [(User.create_date, True)]


class TenantAdmin(ModelView, model=Tenant):
    """租户管理"""
    name = "租户"
    name_plural = "租户管理"
    icon = "fa-solid fa-building"
    
    column_list = [Tenant.id, Tenant.name, Tenant.public_key, Tenant.llm_id, Tenant.embd_id,
                   Tenant.asr_id, Tenant.img2txt_id, Tenant.rerank_id, Tenant.tts_id,
                   Tenant.parser_ids, Tenant.credit, Tenant.status, Tenant.create_date, Tenant.update_date]
    column_searchable_list = [Tenant.name]
    column_sortable_list = [Tenant.create_date, Tenant.credit]
    column_default_sort = [(Tenant.create_date, True)]


class UserTenantAdmin(ModelView, model=UserTenant):
    """用户租户关系"""
    name = "用户租户"
    name_plural = "用户租户关系"
    icon = "fa-solid fa-link"
    
    column_list = [UserTenant.id, UserTenant.user_id, UserTenant.tenant_id, UserTenant.role,
                   UserTenant.invited_by, UserTenant.status, UserTenant.create_date, UserTenant.update_date]
    column_default_sort = [(UserTenant.create_date, True)]


# ========== LLM管理 ==========

class LLMFactoriesAdmin(ModelView, model=LLMFactories):
    """LLM厂商管理"""
    name = "LLM厂商"
    name_plural = "LLM厂商管理"
    icon = "fa-solid fa-industry"
    
    column_list = [LLMFactories.name, LLMFactories.logo, LLMFactories.tags, LLMFactories.status,
                   LLMFactories.id, LLMFactories.create_date, LLMFactories.update_date]
    column_searchable_list = [LLMFactories.name]
    column_sortable_list = [LLMFactories.create_date]
    column_default_sort = [(LLMFactories.create_date, True)]


class LLMAdmin(ModelView, model=LLM):
    """LLM模型管理"""
    name = "LLM模型"
    name_plural = "LLM模型管理"
    icon = "fa-solid fa-brain"
    
    column_list = [LLM.llm_name, LLM.mdl_type, LLM.fid, LLM.max_tokens, LLM.tags,
                   LLM.is_tools, LLM.status, LLM.id, LLM.create_date, LLM.update_date]
    column_searchable_list = [LLM.llm_name, LLM.fid]
    column_sortable_list = [LLM.create_date]
    column_default_sort = [(LLM.create_date, True)]


class TenantLLMAdmin(ModelView, model=TenantLLM):
    """租户LLM配置"""
    name = "租户LLM"
    name_plural = "租户LLM配置"
    icon = "fa-solid fa-cog"
    
    column_list = [TenantLLM.tenant_id, TenantLLM.llm_factory, TenantLLM.mdl_type, TenantLLM.llm_name,
                   TenantLLM.api_base, TenantLLM.max_tokens, TenantLLM.used_tokens, TenantLLM.id,
                   TenantLLM.create_date, TenantLLM.update_date]
    column_default_sort = [(TenantLLM.create_date, True)]
    form_excluded_columns = [TenantLLM.api_key]


class TenantLangfuseAdmin(ModelView, model=TenantLangfuse):
    """租户Langfuse配置"""
    name = "Langfuse配置"
    name_plural = "Langfuse配置管理"
    icon = "fa-solid fa-chart-line"
    
    column_list = [TenantLangfuse.tenant_id, TenantLangfuse.public_key, TenantLangfuse.host,
                   TenantLangfuse.id, TenantLangfuse.create_date, TenantLangfuse.update_date]
    form_excluded_columns = [TenantLangfuse.secret_key]
    column_default_sort = [(TenantLangfuse.create_date, True)]


# ========== AI护栏管理 ==========

class GuardServiceAdmin(ModelView, model=GuardService):
    """AI护栏服务"""
    name = "护栏服务"
    name_plural = "护栏服务管理"
    icon = "fa-solid fa-shield-alt"
    
    column_list = [GuardService.id, GuardService.code, GuardService.name, GuardService.description,
                   GuardService.service_type, GuardService.enabled_dimensions, GuardService.enabled_labels,
                   GuardService.policy_config, GuardService.cache_enabled, GuardService.timeout_ms,
                   GuardService.total_requests, GuardService.blocked_requests, GuardService.tenant_id,
                   GuardService.created_by, GuardService.status, GuardService.create_date, GuardService.update_date]
    column_searchable_list = [GuardService.name, GuardService.code]
    column_sortable_list = [GuardService.create_date, GuardService.total_requests]
    column_default_sort = [(GuardService.create_date, True)]


class GuardServiceLibraryAdmin(ModelView, model=GuardServiceLibrary):
    """护栏服务知识库关联"""
    name = "服务知识库"
    name_plural = "服务知识库关联"
    icon = "fa-solid fa-link"
    
    column_list = [GuardServiceLibrary.id, GuardServiceLibrary.service_id, GuardServiceLibrary.library_id,
                   GuardServiceLibrary.enabled, GuardServiceLibrary.priority, GuardServiceLibrary.library_type,
                   GuardServiceLibrary.apply_to_dimensions, GuardServiceLibrary.apply_to_labels,
                   GuardServiceLibrary.config, GuardServiceLibrary.tenant_id, GuardServiceLibrary.created_by,
                   GuardServiceLibrary.status, GuardServiceLibrary.create_date, GuardServiceLibrary.update_date]
    column_sortable_list = [GuardServiceLibrary.priority]
    column_default_sort = [(GuardServiceLibrary.priority, False)]


class GuardRuleAdmin(ModelView, model=GuardRule):
    """护栏规则"""
    name = "护栏规则"
    name_plural = "护栏规则管理"
    icon = "fa-solid fa-gavel"
    
    column_list = [GuardRule.id, GuardRule.label_id, GuardRule.rule_type, GuardRule.content,
                   GuardRule.content_hash, GuardRule.match_mode, GuardRule.case_sensitive,
                   GuardRule.config, GuardRule.weight, GuardRule.priority, GuardRule.source,
                   GuardRule.description, GuardRule.tenant_id, GuardRule.created_by,
                   GuardRule.status, GuardRule.create_date, GuardRule.update_date]
    column_searchable_list = [GuardRule.content, GuardRule.description]
    column_sortable_list = [GuardRule.create_date, GuardRule.priority]
    column_default_sort = [(GuardRule.priority, False)]
    column_formatters = {
        GuardRule.content: lambda m, a: str(m.content)[:100] + "..." if m.content and len(str(m.content)) > 100 else m.content
    }


class GuardLogAdmin(ModelView, model=GuardLog):
    """护栏日志"""
    name = "护栏日志"
    name_plural = "护栏日志管理"
    icon = "fa-solid fa-file-alt"
    
    column_list = [GuardLog.id, GuardLog.service_id, GuardLog.service_code, GuardLog.request_id,
                   GuardLog.chat_id, GuardLog.user_id, GuardLog.tenant_id, GuardLog.content_hash,
                   GuardLog.content_length, GuardLog.content_preview, GuardLog.is_blocked,
                   GuardLog.risk_score, GuardLog.content_risk_level, GuardLog.content_results,
                   GuardLog.sensitive_level, GuardLog.sensitive_results, GuardLog.attack_level,
                   GuardLog.attack_results, GuardLog.customized_hits, GuardLog.risk_words,
                   GuardLog.sensitive_data, GuardLog.action_taken, GuardLog.action_detail,
                   GuardLog.source_type, GuardLog.source_id, GuardLog.client_ip, GuardLog.user_agent,
                   GuardLog.process_time_ms, GuardLog.cloud_service_used,
                   GuardLog.create_date, GuardLog.update_date]
    column_sortable_list = [GuardLog.create_date, GuardLog.risk_score]
    column_default_sort = [(GuardLog.create_date, True)]
    can_create = False
    can_edit = False
    column_formatters = {
        GuardLog.content_preview: lambda m, a: str(m.content_preview)[:100] + "..." if m.content_preview and len(str(m.content_preview)) > 100 else m.content_preview
    }


class GuardLibraryItemAdmin(ModelView, model=GuardLibraryItem):
    """护栏知识库项"""
    name = "知识库项"
    name_plural = "知识库项管理"
    icon = "fa-solid fa-list"
    
    column_list = [GuardLibraryItem.id, GuardLibraryItem.library_id, GuardLibraryItem.content,
                   GuardLibraryItem.content_hash, GuardLibraryItem.content_type,
                   GuardLibraryItem.item_metadata, GuardLibraryItem.hit_count,
                   GuardLibraryItem.last_hit_time, GuardLibraryItem.sort_order,
                   GuardLibraryItem.tenant_id, GuardLibraryItem.status,
                   GuardLibraryItem.create_date, GuardLibraryItem.update_date]
    column_searchable_list = [GuardLibraryItem.content]
    column_sortable_list = [GuardLibraryItem.create_date, GuardLibraryItem.hit_count, GuardLibraryItem.sort_order]
    column_default_sort = [(GuardLibraryItem.sort_order, False)]
    column_formatters = {
        GuardLibraryItem.content: lambda m, a: str(m.content)[:100] + "..." if m.content and len(str(m.content)) > 100 else m.content
    }


class GuardLibraryAdmin(ModelView, model=GuardLibrary):
    """护栏知识库"""
    name = "护栏知识库"
    name_plural = "护栏知识库管理"
    icon = "fa-solid fa-book-medical"
    
    column_list = [GuardLibrary.id, GuardLibrary.library_type, GuardLibrary.name,
                   GuardLibrary.description, GuardLibrary.category, GuardLibrary.tags,
                   GuardLibrary.config, GuardLibrary.item_count, GuardLibrary.hit_count,
                   GuardLibrary.last_hit_time, GuardLibrary.version, GuardLibrary.tenant_id,
                   GuardLibrary.created_by, GuardLibrary.status,
                   GuardLibrary.create_date, GuardLibrary.update_date]
    column_searchable_list = [GuardLibrary.name, GuardLibrary.description]
    column_sortable_list = [GuardLibrary.create_date, GuardLibrary.item_count, GuardLibrary.hit_count]
    column_default_sort = [(GuardLibrary.create_date, True)]


class GuardLabelAdmin(ModelView, model=GuardLabel):
    """护栏标签"""
    name = "护栏标签"
    name_plural = "护栏标签管理"
    icon = "fa-solid fa-tag"
    
    column_list = [GuardLabel.id, GuardLabel.dimension_id, GuardLabel.code, GuardLabel.name,
                   GuardLabel.description, GuardLabel.cloud_label, GuardLabel.cloud_label_type,
                   GuardLabel.detection_ranges, GuardLabel.enabled, GuardLabel.risk_score,
                   GuardLabel.risk_level, GuardLabel.sensitive_level, GuardLabel.action,
                   GuardLabel.action_config, GuardLabel.sort_order, GuardLabel.tenant_id,
                   GuardLabel.created_by, GuardLabel.status, GuardLabel.create_date, GuardLabel.update_date]
    column_searchable_list = [GuardLabel.name, GuardLabel.code]
    column_sortable_list = [GuardLabel.create_date, GuardLabel.sort_order, GuardLabel.risk_score]
    column_default_sort = [(GuardLabel.sort_order, False)]


class GuardLabelLibraryAdmin(ModelView, model=GuardLabelLibrary):
    """护栏标签知识库关联"""
    name = "标签知识库"
    name_plural = "标签知识库关联"
    icon = "fa-solid fa-link"
    
    column_list = [GuardLabelLibrary.id, GuardLabelLibrary.label_id, GuardLabelLibrary.library_id,
                   GuardLabelLibrary.enabled, GuardLabelLibrary.priority, GuardLabelLibrary.config,
                   GuardLabelLibrary.conditions, GuardLabelLibrary.tenant_id, GuardLabelLibrary.created_by,
                   GuardLabelLibrary.status, GuardLabelLibrary.create_date, GuardLabelLibrary.update_date]
    column_sortable_list = [GuardLabelLibrary.priority]
    column_default_sort = [(GuardLabelLibrary.priority, False)]


class GuardDimensionAdmin(ModelView, model=GuardDimension):
    """护栏维度"""
    name = "护栏维度"
    name_plural = "护栏维度管理"
    icon = "fa-solid fa-cube"
    
    column_list = [GuardDimension.id, GuardDimension.code, GuardDimension.name, GuardDimension.description,
                   GuardDimension.enabled, GuardDimension.config, GuardDimension.sort_order,
                   GuardDimension.tenant_id, GuardDimension.created_by, GuardDimension.status,
                   GuardDimension.create_date, GuardDimension.update_date]
    column_searchable_list = [GuardDimension.name, GuardDimension.code]
    column_sortable_list = [GuardDimension.create_date, GuardDimension.sort_order]
    column_default_sort = [(GuardDimension.sort_order, False)]


# ========== 知识库系统 ==========

class KnowledgebaseAdmin(ModelView, model=Knowledgebase):
    """知识库管理"""
    name = "知识库"
    name_plural = "知识库管理"
    icon = "fa-solid fa-book"
    
    column_list = [Knowledgebase.id, Knowledgebase.avatar, Knowledgebase.tenant_id, Knowledgebase.name,
                   Knowledgebase.language, Knowledgebase.description, Knowledgebase.embd_id,
                   Knowledgebase.permission, Knowledgebase.created_by, Knowledgebase.doc_num,
                   Knowledgebase.token_num, Knowledgebase.chunk_num, Knowledgebase.similarity_threshold,
                   Knowledgebase.vector_similarity_weight, Knowledgebase.parser_id,
                   Knowledgebase.parser_config, Knowledgebase.pagerank, Knowledgebase.status,
                   Knowledgebase.create_date, Knowledgebase.update_date]
    column_searchable_list = [Knowledgebase.name, Knowledgebase.description]
    column_sortable_list = [Knowledgebase.create_date, Knowledgebase.chunk_num, Knowledgebase.doc_num]
    column_default_sort = [(Knowledgebase.create_date, True)]


class DocumentAdmin(ModelView, model=Document):
    """文档管理"""
    name = "文档"
    name_plural = "文档管理"
    icon = "fa-solid fa-file-alt"
    
    column_list = [Document.id, Document.thumbnail, Document.kb_id, Document.parser_id,
                   Document.parser_config, Document.source_type, Document.type, Document.created_by,
                   Document.name, Document.location, Document.size, Document.auth, Document.token_num,
                   Document.chunk_num, Document.progress, Document.progress_msg,
                   Document.process_begin_at, Document.process_duration,
                   Document.suffix, Document.run, Document.status, Document.create_date, Document.update_date]
    column_searchable_list = [Document.name, Document.location]
    column_sortable_list = [Document.create_date, Document.size, Document.progress]
    column_default_sort = [(Document.create_date, True)]
    column_formatters = {
        Document.progress_msg: lambda m, a: str(m.progress_msg)[:100] + "..." if m.progress_msg and len(str(m.progress_msg)) > 100 else m.progress_msg
    }


class FileAdmin(ModelView, model=File):
    """文件管理"""
    name = "文件"
    name_plural = "文件管理"
    icon = "fa-solid fa-folder"
    
    column_list = [File.id, File.parent_id, File.tenant_id, File.created_by, File.name,
                   File.location, File.size, File.type, File.source_type,
                   File.create_date, File.update_date]
    column_searchable_list = [File.name, File.location]
    column_sortable_list = [File.create_date, File.size]
    column_default_sort = [(File.create_date, True)]


class File2DocumentAdmin(ModelView, model=File2Document):
    """文件文档关联"""
    name = "文件文档"
    name_plural = "文件文档关联"
    icon = "fa-solid fa-link"
    
    column_list = [File2Document.id, File2Document.file_id, File2Document.document_id,
                   File2Document.create_date, File2Document.update_date]
    column_default_sort = [(File2Document.create_date, True)]


class TaskAdmin(ModelView, model=Task):
    """任务管理"""
    name = "任务"
    name_plural = "任务管理"
    icon = "fa-solid fa-tasks"
    
    column_list = [Task.id, Task.doc_id, Task.from_page, Task.to_page, Task.task_type,
                   Task.begin_at, Task.process_duration, Task.progress, Task.progress_msg,
                   Task.retry_count, Task.digest, Task.chunk_ids, Task.priority,
                   Task.create_date, Task.update_date]
    column_sortable_list = [Task.create_date, Task.progress, Task.priority]
    column_default_sort = [(Task.priority, True)]
    column_formatters = {
        Task.progress_msg: lambda m, a: str(m.progress_msg)[:100] + "..." if m.progress_msg and len(str(m.progress_msg)) > 100 else m.progress_msg,
        Task.digest: lambda m, a: str(m.digest)[:100] + "..." if m.digest and len(str(m.digest)) > 100 else m.digest
    }


# ========== 对话系统 ==========

class DialogAdmin(ModelView, model=Dialog):
    """对话管理"""
    name = "对话"
    name_plural = "对话管理"
    icon = "fa-solid fa-comment-dots"
    
    column_list = [Dialog.id, Dialog.tenant_id, Dialog.name, Dialog.description, Dialog.icon,
                   Dialog.language, Dialog.llm_id, Dialog.llm_setting, Dialog.prompt_type,
                   Dialog.prompt_config, Dialog.meta_data_filter, Dialog.similarity_threshold,
                   Dialog.vector_similarity_weight, Dialog.top_n, Dialog.top_k, Dialog.do_refer,
                   Dialog.rerank_id, Dialog.kb_ids, Dialog.search_mode, Dialog.status,
                   Dialog.create_date, Dialog.update_date]
    column_searchable_list = [Dialog.name, Dialog.description]
    column_sortable_list = [Dialog.create_date]
    column_default_sort = [(Dialog.create_date, True)]


class ConversationAdmin(ModelView, model=Conversation):
    """会话管理"""
    name = "会话"
    name_plural = "会话管理"
    icon = "fa-solid fa-comments"
    
    column_list = [Conversation.id, Conversation.dialog_id, Conversation.name, Conversation.message,
                   Conversation.reference, Conversation.user_id, Conversation.create_date, Conversation.update_date]
    column_searchable_list = [Conversation.name]
    column_sortable_list = [Conversation.create_date]
    column_default_sort = [(Conversation.create_date, True)]


class APITokenAdmin(ModelView, model=APIToken):
    """API令牌管理"""
    name = "API令牌"
    name_plural = "API令牌管理"
    icon = "fa-solid fa-key"
    
    column_list = [APIToken.tenant_id, APIToken.name, APIToken.description, APIToken.dialog_id,
                   APIToken.source, APIToken.beta, APIToken.id, APIToken.create_date, APIToken.update_date]
    column_searchable_list = [APIToken.name, APIToken.description]
    column_sortable_list = [APIToken.create_date]
    form_excluded_columns = [APIToken.token]
    column_default_sort = [(APIToken.create_date, True)]


class API4ConversationAdmin(ModelView, model=API4Conversation):
    """API会话管理"""
    name = "API会话"
    name_plural = "API会话管理"
    icon = "fa-solid fa-exchange-alt"
    
    column_list = [API4Conversation.id, API4Conversation.dialog_id, API4Conversation.user_id,
                   API4Conversation.message, API4Conversation.reference, API4Conversation.tokens,
                   API4Conversation.source, API4Conversation.dsl, API4Conversation.duration,
                   API4Conversation.round, API4Conversation.thumb_up, API4Conversation.errors,
                   API4Conversation.create_date, API4Conversation.update_date]
    column_sortable_list = [API4Conversation.create_date, API4Conversation.tokens, API4Conversation.duration]
    column_default_sort = [(API4Conversation.create_date, True)]


# ========== Canvas画布系统 ==========

class UserCanvasAdmin(ModelView, model=UserCanvas):
    """用户画布"""
    name = "用户画布"
    name_plural = "用户画布管理"
    icon = "fa-solid fa-palette"
    
    column_list = [UserCanvas.id, UserCanvas.avatar, UserCanvas.user_id, UserCanvas.title,
                   UserCanvas.permission, UserCanvas.description, UserCanvas.canvas_type,
                   UserCanvas.dsl, UserCanvas.create_date, UserCanvas.update_date]
    column_searchable_list = [UserCanvas.title, UserCanvas.description]
    column_sortable_list = [UserCanvas.create_date]
    column_default_sort = [(UserCanvas.create_date, True)]


class CanvasTemplateAdmin(ModelView, model=CanvasTemplate):
    """画布模板"""
    name = "画布模板"
    name_plural = "画布模板管理"
    icon = "fa-solid fa-layer-group"
    
    column_list = [CanvasTemplate.id, CanvasTemplate.avatar, CanvasTemplate.title,
                   CanvasTemplate.description, CanvasTemplate.canvas_type, CanvasTemplate.dsl,
                   CanvasTemplate.create_date, CanvasTemplate.update_date]
    column_searchable_list = [CanvasTemplate.title, CanvasTemplate.description]
    column_sortable_list = [CanvasTemplate.create_date]
    column_default_sort = [(CanvasTemplate.create_date, True)]


class UserCanvasVersionAdmin(ModelView, model=UserCanvasVersion):
    """画布版本"""
    name = "画布版本"
    name_plural = "画布版本管理"
    icon = "fa-solid fa-code-branch"
    
    column_list = [UserCanvasVersion.id, UserCanvasVersion.user_canvas_id, UserCanvasVersion.title,
                   UserCanvasVersion.description, UserCanvasVersion.dsl,
                   UserCanvasVersion.create_date, UserCanvasVersion.update_date]
    column_searchable_list = [UserCanvasVersion.title, UserCanvasVersion.description]
    column_sortable_list = [UserCanvasVersion.create_date]
    column_default_sort = [(UserCanvasVersion.create_date, True)]


# ========== 写作助手系统 ==========

class WritingProjectAdmin(ModelView, model=WritingProject):
    """写作项目"""
    name = "写作项目"
    name_plural = "写作项目管理"
    icon = "fa-solid fa-pen-fancy"
    
    column_list = [WritingProject.id, WritingProject.user_input, WritingProject.content_type,
                   WritingProject.language_style, WritingProject.word_count, WritingProject.reference,
                   WritingProject.model, WritingProject.title, WritingProject.user_id,
                   WritingProject.status, WritingProject.create_date, WritingProject.update_date]
    column_searchable_list = [WritingProject.title, WritingProject.user_input]
    column_sortable_list = [WritingProject.create_date, WritingProject.word_count]
    column_default_sort = [(WritingProject.create_date, True)]
    column_formatters = {
        WritingProject.user_input: lambda m, a: str(m.user_input)[:100] + "..." if m.user_input and len(str(m.user_input)) > 100 else m.user_input
    }


class WritingChapterAdmin(ModelView, model=WritingChapter):
    """写作章节"""
    name = "写作章节"
    name_plural = "写作章节管理"
    icon = "fa-solid fa-book-open"
    
    column_list = [WritingChapter.id, WritingChapter.project_id, WritingChapter.title,
                   WritingChapter.summary, WritingChapter.level, WritingChapter.parent_id,
                   WritingChapter.order_index, WritingChapter.status,
                   WritingChapter.create_date, WritingChapter.update_date]
    column_searchable_list = [WritingChapter.title, WritingChapter.summary]
    column_sortable_list = [WritingChapter.create_date, WritingChapter.order_index]
    column_default_sort = [(WritingChapter.order_index, False)]


class WritingReferenceMaterialAdmin(ModelView, model=WritingReferenceMaterial):
    """写作参考资料"""
    name = "参考资料"
    name_plural = "参考资料管理"
    icon = "fa-solid fa-paperclip"
    
    column_list = [WritingReferenceMaterial.id, WritingReferenceMaterial.chapter_id,
                   WritingReferenceMaterial.title, WritingReferenceMaterial.content,
                   WritingReferenceMaterial.source, WritingReferenceMaterial.type,
                   WritingReferenceMaterial.order_index, WritingReferenceMaterial.status,
                   WritingReferenceMaterial.create_date, WritingReferenceMaterial.update_date]
    column_searchable_list = [WritingReferenceMaterial.title, WritingReferenceMaterial.content]
    column_sortable_list = [WritingReferenceMaterial.create_date, WritingReferenceMaterial.order_index]
    column_default_sort = [(WritingReferenceMaterial.order_index, False)]
    column_formatters = {
        WritingReferenceMaterial.content: lambda m, a: str(m.content)[:100] + "..." if m.content and len(str(m.content)) > 100 else m.content
    }


class WritingChapterContentAdmin(ModelView, model=WritingChapterContent):
    """写作章节内容"""
    name = "章节内容"
    name_plural = "章节内容管理"
    icon = "fa-solid fa-align-left"
    
    column_list = [WritingChapterContent.id, WritingChapterContent.chapter_id, WritingChapterContent.content,
                   WritingChapterContent.status, WritingChapterContent.create_date, WritingChapterContent.update_date]
    column_sortable_list = [WritingChapterContent.create_date]
    column_default_sort = [(WritingChapterContent.create_date, True)]
    column_formatters = {
        WritingChapterContent.content: lambda m, a: str(m.content)[:200] + "..." if m.content and len(str(m.content)) > 200 else m.content
    }


# ========== 数据分析系统 ==========

class AskDataHistoryAdmin(ModelView, model=AskDataHistory):
    """数据问答历史"""
    name = "数据问答"
    name_plural = "数据问答历史"
    icon = "fa-solid fa-chart-bar"
    
    column_list = [AskDataHistory.id, AskDataHistory.conversation_id, AskDataHistory.ask_id,
                   AskDataHistory.user_id, AskDataHistory.data, AskDataHistory.status,
                   AskDataHistory.user_question, AskDataHistory.round_id,
                   AskDataHistory.processed_semantic_layer, AskDataHistory.sql_info,
                   AskDataHistory.create_date, AskDataHistory.update_date]
    column_searchable_list = [AskDataHistory.user_question]
    column_sortable_list = [AskDataHistory.create_date]
    column_default_sort = [(AskDataHistory.create_date, True)]
    column_formatters = {
        AskDataHistory.user_question: lambda m, a: str(m.user_question)[:100] + "..." if m.user_question and len(str(m.user_question)) > 100 else m.user_question,
        AskDataHistory.data: lambda m, a: str(m.data)[:100] + "..." if m.data and len(str(m.data)) > 100 else m.data,
        AskDataHistory.sql_info: lambda m, a: str(m.sql_info)[:100] + "..." if m.sql_info and len(str(m.sql_info)) > 100 else m.sql_info
    }


# ========== API环境管理 ==========

class ApiEnvironmentAdmin(ModelView, model=ApiEnvironment):
    """API环境"""
    name = "API环境"
    name_plural = "API环境管理"
    icon = "fa-solid fa-server"
    
    column_list = [ApiEnvironment.id, ApiEnvironment.tenant_id, ApiEnvironment.name,
                   ApiEnvironment.description, ApiEnvironment.base_url, ApiEnvironment.is_default,
                   ApiEnvironment.is_global, ApiEnvironment.status,
                   ApiEnvironment.create_date, ApiEnvironment.update_date]
    column_searchable_list = [ApiEnvironment.name, ApiEnvironment.description]
    column_sortable_list = [ApiEnvironment.create_date]
    column_default_sort = [(ApiEnvironment.create_date, True)]


class ApiEnvironmentVariableAdmin(ModelView, model=ApiEnvironmentVariable):
    """API环境变量"""
    name = "环境变量"
    name_plural = "环境变量管理"
    icon = "fa-solid fa-code"
    
    column_list = [ApiEnvironmentVariable.id, ApiEnvironmentVariable.environment_id,
                   ApiEnvironmentVariable.key_name, ApiEnvironmentVariable.key_value,
                   ApiEnvironmentVariable.description, ApiEnvironmentVariable.is_secret,
                   ApiEnvironmentVariable.variable_type, ApiEnvironmentVariable.status,
                   ApiEnvironmentVariable.create_date, ApiEnvironmentVariable.update_date]
    column_searchable_list = [ApiEnvironmentVariable.key_name, ApiEnvironmentVariable.description]
    column_sortable_list = [ApiEnvironmentVariable.create_date]
    column_default_sort = [(ApiEnvironmentVariable.create_date, True)]
    column_formatters = {
        ApiEnvironmentVariable.key_value: lambda m, a: "***" if m.is_secret else str(m.key_value)[:50]
    }


class GlobalApiEnvironmentAdmin(ModelView, model=GlobalApiEnvironment):
    """全局API环境"""
    name = "全局环境"
    name_plural = "全局环境管理"
    icon = "fa-solid fa-globe"
    
    column_list = [GlobalApiEnvironment.id, GlobalApiEnvironment.name, GlobalApiEnvironment.description,
                   GlobalApiEnvironment.server_url, GlobalApiEnvironment.variables,
                   GlobalApiEnvironment.is_active, GlobalApiEnvironment.status,
                   GlobalApiEnvironment.create_date, GlobalApiEnvironment.update_date]
    column_searchable_list = [GlobalApiEnvironment.name, GlobalApiEnvironment.description]
    column_sortable_list = [GlobalApiEnvironment.create_date]
    column_default_sort = [(GlobalApiEnvironment.create_date, True)]


# ========== MCP服务器管理 ==========

class MCPServerAdmin(ModelView, model=MCPServer):
    """MCP服务器"""
    name = "MCP服务器"
    name_plural = "MCP服务器管理"
    icon = "fa-solid fa-network-wired"
    
    column_list = [MCPServer.id, MCPServer.name, MCPServer.tenant_id, MCPServer.url,
                   MCPServer.server_type, MCPServer.description, MCPServer.variables,
                   MCPServer.headers, MCPServer.create_date, MCPServer.update_date]
    column_searchable_list = [MCPServer.name, MCPServer.description]
    column_sortable_list = [MCPServer.create_date]
    column_default_sort = [(MCPServer.create_date, True)]


class ToolsDataAdmin(ModelView, model=ToolsData):
    """工具数据"""
    name = "工具数据"
    name_plural = "工具数据管理"
    icon = "fa-solid fa-tools"
    
    column_list = [ToolsData.flow_id, ToolsData.user_id, ToolsData.meta_data,
                   ToolsData.final_data, ToolsData.id, ToolsData.create_date, ToolsData.update_date]
    column_sortable_list = [ToolsData.create_date]
    column_default_sort = [(ToolsData.create_date, True)]
    column_formatters = {
        ToolsData.meta_data: lambda m, a: str(m.meta_data)[:100] + "..." if m.meta_data and len(str(m.meta_data)) > 100 else m.meta_data,
        ToolsData.final_data: lambda m, a: str(m.final_data)[:100] + "..." if m.final_data and len(str(m.final_data)) > 100 else m.final_data
    }


class SearchAdmin(ModelView, model=Search):
    """搜索配置管理"""
    name = "搜索配置"
    name_plural = "搜索配置管理"
    icon = "fa-solid fa-search"
    
    column_list = [Search.id, Search.avatar, Search.tenant_id, Search.name, Search.description,
                   Search.created_by, Search.search_config, Search.status,
                   Search.create_date, Search.update_date]
    column_searchable_list = [Search.name, Search.description]
    column_sortable_list = [Search.create_date]
    column_default_sort = [(Search.create_date, True)]
    column_formatters = {
        Search.description: lambda m, a: str(m.description)[:100] + "..." if m.description and len(str(m.description)) > 100 else m.description
    }
