"""行为钉板：common.settings 的公开访问面（facade 完整性契约）。

配置重构会把 settings.py 改造为 PEP 562 facade。本清单是 2026-07-03 重构前
`vars(settings)` 的全部公开名字（排除模块对象）——重构的任何阶段，这些名字都
必须保持可访问，保证 610 处存量调用点与 ragflow 上游移植 diff 零感知。

如果某个名字被有意下线，必须：同步修改本清单 + 在提交信息里说明 + 全库确认无引用。
"""

from common import settings

# fmt: off
SETTINGS_PUBLIC_NAMES = [
    # ---- 服务与安全 ----
    "HOST_IP", "HOST_PORT", "SECRET_KEY", "ADMIN_REQUIRE_SUPERUSER",
    "REGISTER_ENABLED", "DISABLE_PASSWORD_LOGIN",
    "AUTHENTICATION_CONF", "CLIENT_AUTHENTICATION", "HTTP_APP_KEY",
    "GITHUB_OAUTH", "FEISHU_OAUTH", "OAUTH_CONFIG",
    # ---- LLM 默认模型 ----
    "LLM", "LLM_FACTORY", "LLM_BASE_URL", "API_KEY", "PARSERS",
    "FACTORY_LLM_INFOS", "ALLOWED_LLM_FACTORIES", "BUILTIN_EMBEDDING_MODELS",
    "CHAT_MDL", "EMBEDDING_MDL", "RERANK_MDL", "ASR_MDL", "IMAGE2TEXT_MDL",
    "CHAT_CFG", "EMBEDDING_CFG", "RERANK_CFG", "ASR_CFG", "IMAGE2TEXT_CFG",
    # ---- 文档引擎与后端配置 ----
    "DOC_ENGINE", "DOC_ENGINE_INFINITY", "DOC_ENGINE_OCEANBASE",
    "ES", "MILVUS", "INFINITY", "OS", "OB", "VASTBASE",
    # ---- 存储 ----
    "STORAGE_IMPL", "STORAGE_IMPL_TYPE",
    "AZURE", "S3", "MINIO", "OSS", "GCS",
    # ---- 资源句柄 ----
    "docStoreConn", "msgStoreConn", "retriever", "kg_retriever", "REDIS_CONN",
    # ---- SMTP ----
    "SMTP_CONF", "MAIL_SERVER", "MAIL_PORT", "MAIL_USE_SSL", "MAIL_USE_TLS",
    "MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_DEFAULT_SENDER", "MAIL_FRONTEND_URL",
    # ---- 运行参数 ----
    "LIGHTEN", "PARALLEL_DEVICES", "STRONG_TEST_COUNT",
    "DOC_MAXIMUM_SIZE", "DOC_BULK_SIZE", "EMBEDDING_BATCH_SIZE",
    "SANDBOX_HOST", "RAG_CONF_PATH", "SVR_QUEUE_NAME", "MULTI_RAG_SERVICE_NAME",
    # ---- 杂项服务配置（import 期冻结，9 处直接名字导入依赖）----
    "AIFORBI_BASE_CONFIG", "AIFORBI_BASE_URL", "AIFORBI_API_KEY", "AIFORBI_MODEL_ID",
    "AI_TRANSLATE_BASE_CONFIG", "AI_TRANSLATE_BASE_URL", "AI_TRANSLATE_API_KEY", "AI_TRANSLATE_MODEL_ID",
    "SCRIPT_SCHEDULER_BASE_CONFIG", "SCRIPT_SCHEDULER_HOST", "SCRIPT_SCHEDULER_PORT",
    "DCS_SERVER_BASE_CONFIG", "DCS_SERVER_PROTOCOL", "DCS_SERVER_HOST", "DCS_SERVER_PORT",
    "DCS_SEMANTIC_SERVER_CONFIG", "DCS_SEMANTIC_SERVER_ACCESS_KEY", "DCS_SEMANTIC_SERVER_SECRET_KEY",
    # ---- 类与工厂 ----
    "Storage", "StorageFactory",
    "MultiRAGMinio", "MultiRAGAzureSpnBlob", "MultiRAGAzureSasBlob",
    "MultiRAGS3", "MultiRAGOSS", "MultiRAGGCS", "OpenDALStorage",
    # ---- 函数 ----
    "init_settings", "get_svr_queue_name", "get_svr_queue_names",
    "check_and_install_torch", "print_rag_settings",
    "get_base_config", "decrypt_database_config",
    "get_project_base_directory", "pip_install_torch",
]
# fmt: on


def test_all_public_names_accessible():
    # 资源句柄未初始化时访问会 fail-fast（by design），因此契约检查用
    # dir()（涵盖模块 dict + _LAZY），而非 hasattr（会真的取值）
    available = set(dir(settings))
    missing = [name for name in SETTINGS_PUBLIC_NAMES if name not in available]
    assert not missing, f"settings facade 缺失名字（会破坏存量调用点/上游移植）: {missing}"


def test_inventory_has_no_duplicates():
    assert len(SETTINGS_PUBLIC_NAMES) == len(set(SETTINGS_PUBLIC_NAMES))
