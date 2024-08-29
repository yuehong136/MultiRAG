# coding=utf-8
"""
@project: multirag
@Author：龙
@file： db_models.py
@date：2024/8/7 17:00
@desc:
"""
import os
import sys
import inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy import Column, String, Integer, Float, DateTime, Boolean, Text, ForeignKey, BigInteger, text
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB
from api.db.database import Base, BaseModel, engine
from api.utils.log_utils import getLogger
from alembic import command
from alembic.config import Config

LOGGER = getLogger()


class User(BaseModel):
    __tablename__ = "t_ai_users"
    __table_args__ = {"schema": "local_dev"}  

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    access_token = Column(String(255), index=True, nullable=True)
    nickname = Column(String(100), index=True, nullable=False)
    password = Column(String(255), index=True, nullable=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    avatar = Column(Text, index=False, nullable=True, doc="avatar base64 string")
    language = Column(String(32), index=True, nullable=True, default="English")
    color_schema = Column(String(32), index=True, nullable=True, default="Bright")
    timezone = Column(String(64), index=True, nullable=True, default="UTC+8\tAsia/Shanghai")
    last_login_time = Column(DateTime, index=True, nullable=True)
    is_authenticated = Column(Boolean, index=True, nullable=False, default=True)
    is_active = Column(Boolean, index=True, nullable=False, default=True)
    is_anonymous = Column(Boolean, index=True, nullable=False, default=False)
    login_channel = Column(String, index=True, nullable=True, default=None)
    status = Column(String(1), index=True, nullable=True, default="1")
    is_superuser = Column(Boolean, index=True, nullable=True, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "nickname": self.nickname,
            "last_login_time": self.last_login_time,
            "is_superuser": self.is_superuser,
            # Add other fields as needed
        }


class Tenant(BaseModel):
    __tablename__ = "t_ai_tenants"
    __table_args__ = {"schema": "local_dev"}  

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    name = Column(String(100), index=True, nullable=True, doc="Tenant name")
    public_key = Column(String(255), index=True, nullable=True)
    llm_id = Column(String(128), index=True, nullable=False, doc="default llm ID")
    embd_id = Column(String(128), index=True, nullable=False, doc="default embedding model ID")
    asr_id = Column(String(128), index=True, nullable=False, doc="default ASR model ID")
    img2txt_id = Column(String(128), index=True, nullable=False, doc="default image to text model ID")
    rerank_id = Column(String(128), index=True, nullable=True, doc="default rerank model ID")
    tts_id = Column(String(256), index=True, nullable=True, doc="default tts model ID")
    parser_ids = Column(String(256), index=True, nullable=False, doc="document processors")
    credit = Column(Integer, index=True, nullable=False, default=512)
    status = Column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")

    def to_dict(self):
        return {
            "tenant_id": self.id,
            "name": self.name,
            "llm_id": self.llm_id,
            "embd_id": self.embd_id,
            "rerank_id": self.rerank_id,
            "asr_id": self.asr_id,
            "img2txt_id": self.img2txt_id,
            "parser_ids": self.parser_ids
        }

class UserTenant(BaseModel):
    __tablename__ = "t_ai_user_tenants"
    __table_args__ = {"schema": "local_dev"}  

    id = Column(String(128), primary_key=True, index=False, nullable=False)
    user_id = Column(String(128), index=True, nullable=False)
    tenant_id = Column(String(128), index=True, nullable=False)
    role = Column(String(128), index=True, nullable=False, doc="UserTenantRole")
    invited_by = Column(String(128), index=True, nullable=False)
    status = Column(String(1), index=True, nullable=True, default="1")

    def to_dict(self):
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "role": self.role
        }


# class InvitationCode(Base):
#     __tablename__ = "invitation_codes"
#
#     id = Column(String, primary_key=True, index=True)
#     code = Column(String)
#     visit_time = Column(DateTime)
#     user_id = Column(String, ForeignKey("users.id"))
#     tenant_id = Column(String, ForeignKey("tenants.id"))
#     status = Column(String, default="1")


class LLMFactories(BaseModel):
    __tablename__ = "t_ai_llm_factories"
    __table_args__ = {"schema": "local_dev"}  

    name = Column(String(128), primary_key=True, index=False, nullable=False, doc="LLM factory name")
    logo = Column(Text, index=False, nullable=True)
    tags = Column(String(255), index=True, nullable=False, doc="LLM, Text Embedding, Image2Text, ASR")
    status = Column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")


class LLM(BaseModel):
    __tablename__ = "t_ai_llms"
    __table_args__ = {"schema": "local_dev"}

    llm_name = Column(String(128), primary_key=True, index=True, nullable=False)
    mdl_type = Column(String(128), index=True, nullable=False, doc="LLM, Text Embedding, Image2Text, ASR")
    fid = Column(String(128), index=True, nullable=False, doc="LLM factory id")
    max_tokens = Column(BigInteger, index=False, nullable=False, default=0)
    tags = Column(String(255), index=True, nullable=False, doc="LLM, Text Embedding, Image2Text, Chat, 32k...")
    status = Column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")


class TenantLLM(BaseModel):
    __tablename__ = "t_ai_tenant_llms"
    __table_args__ = {"schema": "local_dev"}  

    tenant_id = Column(String(32), primary_key=True, index=True, nullable=False)
    llm_factory = Column(String(128), primary_key=True, index=True, nullable=False, doc="LLM factory name")
    mdl_type = Column(String(128), index=True, nullable=True, doc="LLM, Text Embedding, Image2Text, ASR")
    llm_name = Column(String(128), primary_key=True, index=True, nullable=True)
    api_key = Column(String(1024), index=True, nullable=True)
    api_base = Column(String(255), index=False, nullable=True)
    used_tokens = Column(Integer, index=True, nullable=False, default=0)


class Knowledgebase(BaseModel):

    __tablename__ = "t_ai_knowledgebases"
    __table_args__ = {"schema": "local_dev"}  


    id = Column(String(32), primary_key=True, index=False, nullable=False)
    avatar = Column(Text, index=False, nullable=True, doc="avatar base64 string")
    tenant_id = Column(String(32), index=True, nullable=False)
    name = Column(String(128), index=True, nullable=False, doc="KB name")
    language = Column(String(32), index=True, nullable=True, default="English", doc="English|Chinese")
    description = Column(Text, index=False, nullable=True, doc="KB description")
    embd_id = Column(String(128), index=True, nullable=False, doc="default embedding model ID")
    permission = Column(String(16), index=True, nullable=False, default="me", doc="me|team")
    created_by = Column(String(32), index=True, nullable=False)
    doc_num = Column(Integer, index=True, nullable=False, default=0)
    token_num = Column(Integer, index=True, nullable=False, default=0)
    chunk_num = Column(Integer, index=True, nullable=False, default=0)
    similarity_threshold = Column(Float, index=True, nullable=False, default=0.2)
    vector_similarity_weight = Column(Float, index=True, nullable=False, default=0.3)
    parser_id = Column(String(32), index=True, nullable=False,doc="default parser ID")
    parser_config = Column(JSONB, index=False, nullable=False, default={"pages": [[1, 1000000]]})
    status = Column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")



class Document(BaseModel):

    __tablename__ = "t_ai_documents"
    __table_args__ = {"schema": "local_dev"}  


    id = Column(String(32), primary_key=True, index=False, nullable=False)
    thumbnail = Column(Text, index=False, nullable=True, doc="thumbnail base64 string")
    kb_id = Column(String(256), index=True, nullable=False)
    parser_id = Column(String(32), index=True, nullable=False, doc="default parser ID")
    parser_config = Column(JSONB, index=False, nullable=False, default={"pages": [[1, 1000000]]})
    source_type = Column(String(128), index=True, nullable=False, default="local", doc="where dose this document come from")
    type = Column(String(32), index=True, nullable=False, doc="file extension")
    created_by = Column(String, index=True, nullable=False, doc="who created it")
    name = Column(String(255), index=True, nullable=True, doc="file name")
    location = Column(String(255), index=True, nullable=True, doc="where dose it store")
    size = Column(Integer, index=True, nullable=False, default=0)
    auth = Column(Text, index=False, nullable=True, doc="attribution of data rights and responsibilities")
    token_num = Column(Integer, index=True, nullable=False, default=0)
    chunk_num = Column(Integer, index=True, nullable=False, default=0)
    progress = Column(Float, index=True, nullable=False, default=0)
    progress_msg = Column(Text, index=False, nullable=True, default="", doc="process message")
    process_begin_at = Column(DateTime, index=True, nullable=True)
    process_duration = Column(Float, index=False, nullable=False, default=0)
    run = Column(String(1), index=True, nullable=True, default="0", doc="start to run processing or cancel.(1: run it; 2: cancel)")
    status = Column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")



class File(BaseModel):
    __tablename__ = "t_ai_files"
    __table_args__ = {"schema": "local_dev"}  


    id = Column(String(32), primary_key=True, index=False, nullable=False)
    parent_id = Column(String(32), index=True, nullable=False, doc="parent folder id")
    tenant_id = Column(String(32), index=True, nullable=False, doc="tenant id")
    created_by = Column(String(32), index=True, nullable=False, doc="who created it")
    name = Column(String(255), index=True, nullable=False, doc="file name or folder name")
    location = Column(String(255), index=True, nullable=True, doc="where dose it store")
    size = Column(Integer, index=True, nullable=False, default=0)
    type = Column(String(32), index=True, nullable=False)
    source_type = Column(String(128), index=True, nullable=False, default="", doc="where dose this document come from")

    def to_dict(self):
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "tenant_id": self.tenant_id,
            "created_by": self.created_by,
            "name": self.name,
            "location": self.location,
            "size": self.size,
            "type": self.type,
            "source_type": self.source_type
        }


class File2Document(BaseModel):
    __tablename__ = "t_ai_file2documents"
    __table_args__ = {"schema": "local_dev"}  


    id = Column(String(32), primary_key=True, index=False, nullable=False)
    file_id = Column(String(32), index=True, nullable=True, doc="file id")
    document_id = Column(String(32), index=True, nullable=True, doc="document id")


class Task(BaseModel):
    __tablename__ = "t_ai_tasks"
    __table_args__ = {"schema": "local_dev"}  


    id = Column(String(32), primary_key=True, index=False, nullable=False)
    doc_id = Column(String(32), index=True, nullable=False)
    from_page = Column(Integer, index=False, nullable=False, default=0)
    to_page = Column(Integer, index=False, nullable=False, default=-1)
    begin_at = Column(DateTime, index=True, nullable=True)
    process_duration = Column(Float, index=False, nullable=False, default=0)
    progress = Column(Float, index=True, nullable=False, default=0)
    progress_msg = Column(Text, index=False, nullable=True, default="", doc="process message")


class Dialog(BaseModel):
    __tablename__ = "t_ai_dialogs"
    __table_args__ = {"schema": "local_dev"}  

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    tenant_id = Column(String(32), index=True, nullable=False)
    name = Column(String(255), index=True, nullable=True, doc="dialog application name")
    description = Column(Text, index=False, nullable=True, doc="Dialog description")
    icon = Column(Text, index=False, nullable=True, doc="icon base64 string")
    language = Column(String(32), index=True, nullable=True, default="English", doc="English|Chinese")
    llm_id = Column(String(128), index=False, nullable=False, doc="default llm ID")
    llm_setting = Column(JSONB, index=False, nullable=False,
                         default={"temperature": 0.1, "top_p": 0.3, "frequency_penalty": 0.7, "presence_penalty": 0.4,
                                  "max_tokens": 512})
    prompt_type = Column(String(16), index=True, nullable=False, default="simple", doc="simple|advanced")
    prompt_config = Column(JSONB, index=False, nullable=False,
                           default={"system": "", "prologue": "您好，我是您的助手小樱，长得可爱又善良，can I help you?",
                                    "parameters": [], "empty_response": "Sorry! 知识库中未找到相关内容！"})
    similarity_threshold = Column(Float, index=False, nullable=False, default=0.2)
    vector_similarity_weight = Column(Float, index=False, nullable=False, default=0.3)
    top_n = Column(Integer, index=False, nullable=False, default=6)
    top_k = Column(Integer, index=False, nullable=False, default=1024)
    do_refer = Column(String(1), index=False, nullable=False, default="1", doc="it needs to insert reference index into answer or not")
    rerank_id = Column(String(128), index=False, nullable=True, doc="default rerank model ID")
    kb_ids = Column(JSONB, index=False, nullable=False, default=[])
    status = Column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")


class Conversation(BaseModel):
    __tablename__ = "t_ai_conversations"
    __table_args__ = {"schema": "local_dev"}  

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    dialog_id = Column(String(32), index=True, nullable=False)
    name = Column(String(255), index=True, nullable=True, doc="converastion name")
    message = Column(JSONB, index=False, nullable=True)
    reference = Column(JSONB, index=False, nullable=True, default=[])


class APIToken(BaseModel):
    __tablename__ = "t_ai_api_tokens"
    __table_args__ = {"schema": "local_dev"}  

    tenant_id = Column(String(32), primary_key=True, index=True, nullable=False)
    token = Column(String(255), primary_key=True, index=True, nullable=False)
    dialog_id = Column(String(32), index=True, nullable=False)
    source = Column(String(16), index=True, nullable=True, doc="none|agent|dialog")


class API4Conversation(BaseModel):
    __tablename__ = "t_ai_api4conversations"
    __table_args__ = {"schema": "local_dev"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    dialog_id = Column(String(32), index=True, nullable=False)
    user_id = Column(String(255), index=True, nullable=False, doc="user_id")
    message = Column(JSONB, index=False, nullable=True)
    reference = Column(JSONB, index=False, nullable=True, default=[])
    tokens = Column(Integer, index=False, nullable=False, default=0)
    source = Column(String(16), index=True, nullable=True, doc="none|agent|dialog")
    duration = Column(Float, index=True, nullable=False, default=0)
    round = Column(Integer, index=True, nullable=False, default=0)
    thumb_up = Column(Integer, index=True, nullable=False, default=0)


class UserCanvas(BaseModel):
    __tablename__ = "t_ai_user_canvases"
    __table_args__ = {"schema": "local_dev"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    avatar = Column(Text, index=False, nullable=True, doc="avatar base64 string")
    user_id = Column(String(255), index=True, nullable=False, doc="user_id")
    title = Column(String(255), index=False, nullable=True, doc="Canvas title")
    description = Column(Text, index=False, nullable=True, doc="Canvas description")
    canvas_type = Column(String(32), index=True, nullable=True, doc="Canvas type")
    dsl = Column(JSONB, index=False, nullable=True, default={})


class CanvasTemplate(BaseModel):
    __tablename__ = "t_ai_canvas_templates"
    __table_args__ = {"schema": "local_dev"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    avatar = Column(Text, index=False, nullable=True, doc="avatar base64 string")
    title = Column(String(255), index=False, nullable=True, doc="Canvas title")
    description = Column(Text, index=False, nullable=True, doc="Canvas description")
    canvas_type = Column(String(32), index=True, nullable=True, doc="Canvas type")
    dsl = Column(JSONB, index=False, nullable=True, default={})


def init_database_tables():
    # 需要创建的 schema 名称
    schema_name = 'local_dev'

    # 检查并创建 schema
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_name}"))

        connection.execute(text("COMMIT"))  # 提交创建schema的事务

    # # 构建相对路径到 alembic.ini 和迁移脚本目录
    # current_dir = os.path.dirname(__file__)
    # alembic_ini_path = os.path.join(current_dir, '..', '..', 'configs', 'alembic.ini')
    # migrations_path = os.path.join(current_dir, '..', '..', 'configs', 'alembic')
    #
    # # 执行 Alembic 迁移
    # alembic_cfg = Config(r"E:\Project\python\study\RAG\configs\alembic.ini")
    # print("Generated SQLAlchemy URL:", str(engine.url))
    # alembic_cfg.set_main_option("sqlalchemy.url", str(engine.url))
    # alembic_cfg.set_main_option("script_location", r"E:\Project\python\study\RAG\configs\alembic")
    #
    # try:
    #     LOGGER.info("Starting Alembic migration...")
    #     command.upgrade(alembic_cfg, "head")
    #     LOGGER.info("Alembic migration completed successfully.")
    # except UnicodeDecodeError as e:
    #     LOGGER.error(f"UnicodeDecodeError: {e}")
    #     raise
    # except Exception as e:
    #     LOGGER.exception(f"Alembic migration failed: {e}")
    #     raise

    # 获取现有表列表
    inspector = sa_inspect(engine)
    existing_tables = inspector.get_table_names(schema=schema_name)
    members = inspect.getmembers(sys.modules[__name__], inspect.isclass)
    table_objs = []
    create_failed_list = []

    for name, obj in members:
        if obj != BaseModel and issubclass(obj, BaseModel):
            table_objs.append(obj)
            LOGGER.info(f"Start creating table {obj.__name__} in schema {schema_name}")
            try:
                # 检查表是否存在并创建表
                if obj.__tablename__ not in existing_tables:
                    obj.__table__.create(bind=engine, checkfirst=True)
                    LOGGER.info(f"Successfully created table: {obj.__name__}")
            except OperationalError as e:
                LOGGER.exception(f"Error creating table {obj.__name__}: {e}")
                create_failed_list.append(obj.__name__)

    if create_failed_list:
        LOGGER.error(f"Failed to create tables: {create_failed_list}")
        raise Exception(f"Failed to create tables: {create_failed_list}")
