import asyncio
import json
import logging
import os
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from api.db import UserTenantRole
from api.db.db_models import LLM, Dialog, GuardDimension, Knowledgebase, LLMFactories, Memory, TenantLLM, db_connection
from api.db.joint_services.memory_message_service import fix_missing_tokenized_memory, init_memory_size_cache, init_message_id_sequence
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from api.db.schema_bootstrap import bootstrap_database_schema
from api.db.services import UserService
from api.db.services.canvas_service import CanvasTemplateService
from api.db.services.dialog_service import DialogService
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle, LLMService
from api.db.services.memory_service import MemoryService
from api.db.services.system_settings_service import SystemSettingsService
from api.db.services.tenant_llm_service import LLMFactoriesService, TenantLLMService
from api.db.services.user_service import TenantService
from api.db.template_utils import normalize_canvas_template_categories

# from api.common.base64 import encode_to_base64
from common import settings
from common.constants import LLMType
from common.file_utils import get_project_base_directory
from scripts.init_ai_guard_system import init_ai_guard_system

# 超级用户默认配置（支持环境变量覆盖）
DEFAULT_SUPERUSER_NICKNAME = os.getenv("DEFAULT_SUPERUSER_NICKNAME", "admin")
DEFAULT_SUPERUSER_EMAIL = os.getenv("DEFAULT_SUPERUSER_EMAIL", "admin@datav.com")
DEFAULT_SUPERUSER_PASSWORD = os.getenv("DEFAULT_SUPERUSER_PASSWORD", "admin")


def init_superuser(
    db: Session, nickname: str = DEFAULT_SUPERUSER_NICKNAME, email: str = DEFAULT_SUPERUSER_EMAIL, password: str = DEFAULT_SUPERUSER_PASSWORD, role: UserTenantRole = UserTenantRole.OWNER
) -> None:
    """
    初始化超级用户

    Args:
        db: 数据库会话
        nickname: 用户昵称，默认从环境变量 DEFAULT_SUPERUSER_NICKNAME 读取
        email: 用户邮箱，默认从环境变量 DEFAULT_SUPERUSER_EMAIL 读取
        password: 用户密码，默认从环境变量 DEFAULT_SUPERUSER_PASSWORD 读取
        role: 用户角色，默认为 OWNER
    """
    # 检查是否已存在该邮箱的用户
    existing_admin = UserService.query_user_onlywith_email(db, email)
    if existing_admin:
        logging.info(f"超级用户已存在（邮箱：{email}），跳过初始化")
        return

    user_info = {
        "id": uuid.uuid1().hex,
        "password": password,
        "nickname": nickname,
        "is_superuser": True,
        "email": email,
        "status": "1",
    }
    from api.db.joint_services.user_account_service import initialize_user_tenant

    try:
        UserService.save(db, commit=False, **user_info)
        tenant_object = initialize_user_tenant(db, user_info, role)
        tenant = tenant_object.to_dict()
        db.commit()
    except Exception:
        db.rollback()
        raise

    logging.info(f"Super user initialized. email: {email},A default password has been set; changing the password after login is strongly recommended.")

    if tenant["llm_id"]:
        chat_config = get_model_config_by_type_and_name(db, tenant["id"], LLMType.CHAT.value, tenant["llm_id"])
        chat_mdl = LLMBundle(db, tenant["id"], chat_config)
        msg = asyncio.run(chat_mdl.async_chat(system="", history=[{"role": "user", "content": "Hello!"}], gen_conf={}))
        if msg.find("ERROR: ") == 0:
            logging.error("'{}' doesn't work. {}".format(tenant["llm_id"], msg))

    if tenant["embd_id"]:
        embd_config = get_model_config_by_type_and_name(db, tenant["id"], LLMType.EMBEDDING.value, tenant["embd_id"])
        embd_mdl = LLMBundle(db, tenant["id"], embd_config)
        v, c = embd_mdl.encode(["Hello!"])
        if c == 0:
            logging.error("'{}' doesn't work!".format(tenant["embd_id"]))


def init_llm_factory(db: Session) -> None:
    from api.db.services.model_provider_service import catalog

    # The modern catalog is authoritative. The legacy table keeps a primary
    # capability and IMAGE2TEXT tag for the active admin read projection.
    with db.begin_nested():
        db.execute(delete(LLMFactories))
        db.execute(delete(LLM))
        factory_fields = set(LLMFactories.__table__.columns.keys())
        for factory in settings.FACTORY_LLM_INFOS:
            db.add(LLMFactories(**{key: value for key, value in factory.items() if key in factory_fields}))
            for model in catalog(factory["name"]):
                raw = next(item for item in factory["llm"] if item["llm_name"] == model.model_name)
                tags = list(dict.fromkeys(tag.strip() for tag in raw.get("tags", "").split(",") if tag.strip()))
                if "image2text" in model.model_types and "IMAGE2TEXT" not in tags:
                    tags.append("IMAGE2TEXT")
                db.add(LLM(fid=factory["name"], llm_name=model.model_name, mdl_type=model.model_types[0], max_tokens=model.max_tokens, tags=",".join(tags), is_tools=model.is_tools))
        db.flush()
    db.commit()

    LLMFactoriesService.filter_delete(db, [(LLMFactories.name == "Local") | (LLMFactories.name == "novita.ai")])
    LLMService.filter_delete(db, [LLM.fid == "Local"])
    LLMService.filter_delete(db, [LLM.llm_name == "qwen-vl-max"])
    LLMService.filter_delete(db, [LLM.fid == "Moonshot", LLM.llm_name == "flag-embedding"])
    TenantLLMService.filter_delete(db, [TenantLLM.llm_factory == "Moonshot", TenantLLM.llm_name == "flag-embedding"])
    LLMFactoriesService.filter_delete(db, [LLMFactories.name == "QAnything"])
    LLMService.filter_delete(db, [LLM.fid == "QAnything"])
    TenantLLMService.filter_update(db, [TenantLLM.llm_factory == "QAnything"], {"llm_factory": "Youdao"})
    TenantLLMService.filter_update(db, [TenantLLM.llm_factory == "cohere"], {"llm_factory": "Cohere"})
    TenantService.filter_update(
        db,
        [1 == 1],
        {
            "parser_ids": "naive:General,qa:Q&A,resume:Resume,manual:Manual,table:Table,paper:Paper,book:Book,laws:Laws,presentation:Presentation,picture:Picture,one:One,audio:Audio,email:Email,tag:Tag"
        },
    )

    doc_count = DocumentService.get_all_kb_doc_count(db)
    for kb_id in KnowledgebaseService.get_all_ids(db):
        KnowledgebaseService.update_document_number_in_init(db, kb_id=kb_id, doc_num=doc_count.get(kb_id, 0))


def _batch_fill_tenant_model_id(db: Session, rows, *, model_name_attr: str, target_field: str, service, model_class):
    """按 (tenant_id, model_name) 分组，每组查一次 get_api_key，批量更新。"""
    groups: dict[tuple[str, str], list] = {}
    for row in rows:
        key = (row.tenant_id, getattr(row, model_name_attr))
        groups.setdefault(key, []).append(row.id)

    for (tenant_id, model_name), ids in groups.items():
        tenant_llm = TenantLLMService.get_api_key(db, tenant_id, model_name)
        if tenant_llm:
            service.filter_update(db, [model_class.id.in_(ids)], {target_field: tenant_llm.id})


def fix_empty_tenant_model_id(db: Session) -> None:
    from api.db.model_backfill import backfill_tenant_defaults

    backfill_tenant_defaults(db)
    db.commit()

    _batch_fill_tenant_model_id(
        db,
        KnowledgebaseService.get_null_tenant_embd_id_row(db),
        model_name_attr="embd_id",
        target_field="tenant_embd_id",
        service=KnowledgebaseService,
        model_class=Knowledgebase,
    )
    _batch_fill_tenant_model_id(
        db,
        DialogService.get_null_tenant_llm_id_row(db),
        model_name_attr="llm_id",
        target_field="tenant_llm_id",
        service=DialogService,
        model_class=Dialog,
    )
    if hasattr(DialogService, "get_null_tenant_rerank_id_row"):
        _batch_fill_tenant_model_id(
            db,
            DialogService.get_null_tenant_rerank_id_row(db),
            model_name_attr="rerank_id",
            target_field="tenant_rerank_id",
            service=DialogService,
            model_class=Dialog,
        )
    _batch_fill_tenant_model_id(
        db,
        MemoryService.get_null_tenant_embd_id_row(db),
        model_name_attr="embd_id",
        target_field="tenant_embd_id",
        service=MemoryService,
        model_class=Memory,
    )
    _batch_fill_tenant_model_id(
        db,
        MemoryService.get_null_tenant_llm_id_row(db),
        model_name_attr="llm_id",
        target_field="tenant_llm_id",
        service=MemoryService,
        model_class=Memory,
    )


def add_graph_templates(db: Session):
    dir = os.path.join(get_project_base_directory(), "agent", "templates")
    CanvasTemplateService.filter_delete(db, [1 == 1])
    if not os.path.exists(dir):
        logging.warning("Missing agent templates!")
        return

    for fnm in sorted(os.listdir(dir)):
        if not fnm.endswith(".json"):
            logging.debug("Skipping non-json template file in %s: %s", dir, fnm)
            continue
        template_path = os.path.join(dir, fnm)
        try:
            with open(template_path, encoding="utf-8") as f:
                cnvs = normalize_canvas_template_categories(json.load(f))
            logging.info("Loaded and normalized template file: %s", template_path)

            # 将 ID 转换为字符串以确保一致性
            cnvs["id"] = str(cnvs["id"])

            # 在插入前检查记录是否已存在
            existing_template = None
            try:
                existing_template = CanvasTemplateService.get_by_id(db, cnvs["id"])
            except HTTPException as e:
                if e.status_code == 404:
                    # 记录不存在，继续执行插入逻辑
                    logging.debug("Template %s not found, will insert.", cnvs["id"])
                else:
                    raise  # 其他异常重新抛出
            if existing_template:
                # 如果记录存在，则更新
                try:
                    CanvasTemplateService.update_by_id(db, cnvs["id"], cnvs)
                except Exception as e:
                    # print(f"Error updating template {cnvs['id']}: {e}")
                    logging.exception(f"Error updating template {cnvs['id']}: {e}")
                    db.rollback()  # 回滚事务
            else:
                # 如果记录不存在，则插入
                try:
                    CanvasTemplateService.save(db, **cnvs)
                except Exception as e:
                    # print(f"Error saving template {cnvs['id']}: {e}")
                    logging.exception(f"Error saving template {cnvs['id']}: {e}")
                    db.rollback()  # 回滚事务

        except Exception as e:
            logging.exception(f"Add agent templates error for {template_path}: {e}")

            db.rollback()  # 回滚事务


def init_guard_system_wrapper(db: Session, tenant_id: str, user_id: str):
    """
    初始化AI安全护栏系统（仅在未初始化时执行）

    Args:
        db: 数据库会话
        tenant_id: 租户ID
        user_id: 用户ID
    """
    try:
        # 检查是否已有任何租户初始化过（由于code字段全局唯一，只能初始化一次）
        existing_dimensions = db.query(GuardDimension).count()

        if existing_dimensions > 0:
            logging.info(f"AI安全护栏系统已初始化（发现 {existing_dimensions} 个维度），跳过初始化")
            return

        logging.info(f"开始初始化AI安全护栏系统，租户: {tenant_id}, 用户: {user_id}")

        # 使用 init_ai_guard_system 进行初始化
        init_ai_guard_system(tenant_id=tenant_id, created_by=user_id)

        logging.info("✅ AI安全护栏系统初始化完成")

    except Exception as e:
        logging.error(f"初始化AI安全护栏系统失败: {e}")
        raise


def init_table(db: Session):
    """从 configs/system_settings.json 初始化系统设置（幂等）"""
    settings_file = os.path.join(get_project_base_directory(), "configs", "system_settings.json")
    if not os.path.exists(settings_file):
        logging.warning("Missing system_settings.json, skipping system settings initialization")
        return

    with open(settings_file, encoding="utf-8") as f:
        records_from_file = json.load(f)["system_settings"]

    # 获取已有记录，构建索引
    records_from_db = SystemSettingsService.get_all(db)
    existing_names = {record.name for record in records_from_db}

    # 筛选出需要新增的记录
    to_save = [record for record in records_from_file if record["name"] not in existing_names]

    if to_save:
        try:
            SystemSettingsService.insert_many(db, to_save, len(to_save))
            logging.info(f"Initialized {len(to_save)} system settings")
        except Exception as e:
            logging.exception(f"System settings init error: {e}")
            raise


def _init_web_data_with_db(db: Session) -> None:
    start_time = time.time()
    try:
        init_table(db)

        init_llm_factory(db)

        # 获取所有用户（只查询一次）
        all_users = UserService.get_all(db)

        # 如果没有用户，创建超级用户
        if len(all_users) == 0:
            # 额外检查：通过邮箱查询是否已经存在超级用户
            existing_admin = UserService.query_user_onlywith_email(db, DEFAULT_SUPERUSER_EMAIL)
            if not existing_admin:
                init_superuser(db)
                # 重新获取用户列表
                all_users = UserService.get_all(db)
            else:
                logging.info("超级用户已存在，跳过初始化")
                all_users = [existing_admin]

        for tenant_id in db.scalars(select(TenantService.model.id).order_by(TenantService.model.id)):
            TenantLLMService.ensure_ocr_models_from_env(db, tenant_id)
        db.commit()

        add_graph_templates(db)

        # 初始化AI安全护栏系统
        # 检查是否已有guard数据
        existing_guard_data = db.query(GuardDimension).count()
        if existing_guard_data == 0:
            # 未初始化，查找超级用户
            superuser = next((user for user in all_users if user.is_superuser), None)

            if superuser:
                logging.info(f"使用超级用户 {superuser.id} 初始化AI安全护栏系统")
                init_guard_system_wrapper(db, tenant_id=superuser.id, user_id=superuser.id)
            else:
                logging.warning("未找到超级用户，跳过AI安全护栏系统初始化")
        else:
            logging.info("AI安全护栏系统已存在数据，跳过初始化")

        # Initialize message ID sequence and memory size cache
        init_message_id_sequence(db)
        init_memory_size_cache(db)
        fix_empty_tenant_model_id(db)
        fix_missing_tokenized_memory(db)

        logging.info(f"init web data success:{time.time() - start_time}")
    except Exception:
        # 对外部传入的 db：这里 rollback 只负责清理当前函数内造成的脏事务状态
        # （具体 commit 发生在各 Service 内部）
        try:
            db.rollback()
        except Exception:
            pass
        raise


def init_web_data(db: Session | None = None):
    """
    初始化默认数据。

    为什么默认走 `db_connection()`：
    - 统一会话创建/异常回滚/关闭的逻辑
    - 避免 `SessionLocal()` 作为默认参数在 import 阶段就创建 Session（多进程部署会踩坑）
    - 仍支持外部传入 db（比如想复用同一个 session 做一串初始化）
    """
    if db is None:
        with db_connection() as _db:
            return _init_web_data_with_db(_db)
    return _init_web_data_with_db(db)


if __name__ == "__main__":
    bootstrap_database_schema()
    init_web_data()
