# coding=utf-8
"""
@project: multirag
@Author：龙
@file： db_models.py
@date：2024/8/7 17:00
@desc:
"""
import sys
import inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy import Column, String, Integer, Float, DateTime, Boolean, Text, ForeignKey, BigInteger, text
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB
from api.db.database import Base, BaseModel, engine
from api.utils.log_utils import getLogger

LOGGER = getLogger()

# todo ragflow表字段都添加了索引: index=True


class User(BaseModel):
    __tablename__ = "t_ai_users"
    __table_args__ = {"schema": "test_dve"}  # 使用public schema

    id = Column(String, primary_key=True, index=True)
    access_token = Column(String, index=True)
    nickname = Column(String)
    password = Column(String)
    email = Column(String, unique=True, index=True)
    # avatar = Column(Text)
    # language = Column(String, default="English")
    # color_schema = Column(String, default="Bright")
    # timezone = Column(String, default="UTC+8\tAsia/Shanghai")
    last_login_time = Column(DateTime)
    is_authenticated = Column(Boolean, default=True)
    is_active = Column(Boolean, default=True)
    # is_anonymous = Column(Boolean, default=False)
    # login_channel = Column(String)
    status = Column(String, default="1")
    is_superuser = Column(Boolean, default=False)

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
    __table_args__ = {"schema": "test_dve"}  # 使用public schema

    id = Column(String, primary_key=True, index=True)
    name = Column(String)
    public_key = Column(String)
    llm_id = Column(String)
    embd_id = Column(String)
    asr_id = Column(String)
    img2txt_id = Column(String)
    rerank_id = Column(String)
    parser_ids = Column(String)
    credit = Column(Integer, default=512)
    status = Column(String, default="1")

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
    __table_args__ = {"schema": "test_dve"}  # 使用public schema

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String)
    tenant_id = Column(String)
    role = Column(String)
    invited_by = Column(String)
    status = Column(String, default="1")

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
    __table_args__ = {"schema": "test_dve"}  # 使用public schema

    name = Column(String, primary_key=True, index=True)
    logo = Column(Text)
    tags = Column(String)
    status = Column(String, default="1")


class LLM(BaseModel):
    __tablename__ = "t_ai_llms"
    __table_args__ = {"schema": "test_dve"}

    llm_name = Column(String, primary_key=True, index=True, nullable=False)
    mdl_type = Column(String, nullable=False)
    fid = Column(String, nullable=False)
    max_tokens = Column(BigInteger, default=0, nullable=False)
    tags = Column(String, nullable=False)
    status = Column(String, default="1", nullable=False)


class TenantLLM(BaseModel):
    __tablename__ = "t_ai_tenant_llms"
    __table_args__ = {"schema": "test_dve"}  # 使用public schema

    tenant_id = Column(String, primary_key=True)
    llm_factory = Column(String, primary_key=True)
    mdl_type = Column(String)
    llm_name = Column(String, primary_key=True)
    # api_key = Column(String)
    api_key = Column(String(1024))
    api_base = Column(String)
    used_tokens = Column(Integer, default=0)


class Knowledgebase(BaseModel):
    """
    知识库实体类，用于表示知识库的结构和属性。

    Attributes:
        __tablename__: 表名，知识库在数据库中的表名。
        id: 主键，知识库的唯一标识符。
        avatar: 头像，知识库的图标或图片。
        tenant_id: 租户ID，知识库所属的租户的唯一标识符。
        name: 名称，知识库的名称。
        language: 语言，知识库使用的语言，默认为"English"。
        description: 描述，对知识库的简要描述。
        embd_id: 嵌入ID，与知识库相关的嵌入向量的标识符。
        permission: 权限，知识库的访问权限，默认为"me"，表示个人权限。
        created_by: 创建者，知识库的创建者。
        doc_num: 文档数，知识库中的文档数量，默认为0。
        token_num: 令牌数，知识库中的令牌数量，默认为0。
        chunk_num: 块数，知识库中的数据块数量，默认为0。
        similarity_threshold: 相似度阈值，用于判断两个知识库之间相似度的阈值，默认为0.2。
        vector_similarity_weight: 向量相似度权重，用于计算知识库相似度时的向量相似度的权重，默认为0.3。
        parser_id: 解析器ID，用于解析知识库内容的解析器的标识符。
        parser_config: 解析器配置，解析器的配置信息，以JSONB格式存储，默认配置为{"pages": [[1, 1000000]]}，表示解析所有页面。
        status: 状态，知识库的状态，默认为"1"，表示正常状态。
    """
    __tablename__ = "t_ai_knowledgebases"
    __table_args__ = {"schema": "test_dve"}  # 使用public schema


    id = Column(String, primary_key=True, index=True)
    avatar = Column(Text)
    tenant_id = Column(String)
    name = Column(String, index=True)
    language = Column(String, default="English")
    description = Column(Text)
    embd_id = Column(String)
    permission = Column(String, default="me")
    created_by = Column(String)
    doc_num = Column(Integer, default=0)
    token_num = Column(Integer, default=0)
    chunk_num = Column(Integer, default=0)
    similarity_threshold = Column(Float, default=0.2)
    vector_similarity_weight = Column(Float, default=0.3)
    parser_id = Column(String)
    parser_config = Column(JSONB, default={"pages": [[1, 1000000]]})
    status = Column(String, default="1")



class Document(BaseModel):
    """
    文档实体类，代表一个文档的信息。

    Attributes:
        __tablename__: 表名。
        id: 文档主键，唯一标识一个文档。
        thumbnail: 文档缩略图。
        kb_id: 知识库ID，文档所属的知识库的外键。
        parser_id: 解析器ID，标识使用哪个解析器处理文档。
        parser_config: 解析器配置，默认配置为解析第1页到第1000000页。
        source_type: 文档来源类型，默认为"local"表示本地上传。
        type: 文档类型。
        created_by: 创建者，标识文档的创建者。
        name: 文档名称。
        location: 文档存储位置。
        size: 文档大小，以字节为单位。
        token_num: 文档的 token 数量。
        chunk_num: 文档的 chunk 数量。
        progress: 文档处理进度，以浮点数表示。
        progress_msg: 文档处理进度信息。
        process_begin_at: 文档处理开始时间。
        process_duration: 文档处理时长，以浮点数表示。
        run: 文档处理运行标识，默认为"0"。
        status: 文档状态，默认为"1"表示正常。
    """
    __tablename__ = "t_ai_documents"
    __table_args__ = {"schema": "test_dve"}  # 使用public schema


    id = Column(String, primary_key=True, index=True)
    thumbnail = Column(Text)
    kb_id = Column(String, index=True)
    parser_id = Column(String)
    parser_config = Column(JSONB, default={"pages": [[1, 1000000]]})
    source_type = Column(String, default="local")
    type = Column(String)
    created_by = Column(String)
    name = Column(String, index=True)
    location = Column(String)
    size = Column(Integer, default=0)
    token_num = Column(Integer, default=0)
    chunk_num = Column(Integer, default=0)
    progress = Column(Float, default=0)
    progress_msg = Column(Text, default="")
    process_begin_at = Column(DateTime)
    process_duration = Column(Float, default=0)
    run = Column(String, default="0")
    status = Column(String, default="1")



class File(BaseModel):
    __tablename__ = "t_ai_files"
    __table_args__ = {"schema": "test_dve"}  # 使用public schema


    id = Column(String, primary_key=True, index=True)
    parent_id = Column(String, index=True)
    tenant_id = Column(String, index=True)
    created_by = Column(String)
    name = Column(String, index=True)
    location = Column(String)
    size = Column(Integer, default=0)
    type = Column(String)
    source_type = Column(String, default="")

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
    __table_args__ = {"schema": "test_dve"}  # 使用public schema


    id = Column(String, primary_key=True, index=True)
    file_id = Column(String, index=True)
    document_id = Column(String, index=True)


class Task(BaseModel):
    __tablename__ = "t_ai_tasks"
    __table_args__ = {"schema": "test_dve"}  # 使用public schema


    id = Column(String, primary_key=True, index=True)
    doc_id = Column(String, index=True)
    from_page = Column(Integer, default=0)
    to_page = Column(Integer, default=-1)
    begin_at = Column(DateTime)
    process_duration = Column(Float, default=0)
    progress = Column(Float, default=0)
    progress_msg = Column(Text, default="")


class Dialog(BaseModel):
    __tablename__ = "t_ai_dialogs"
    __table_args__ = {"schema": "test_dve"}  # 使用public schema

    id = Column(String, primary_key=True, index=True)
    tenant_id = Column(String)
    name = Column(String)
    description = Column(Text)
    icon = Column(Text)
    language = Column(String, default="English")
    llm_id = Column(String)
    llm_setting = Column(JSONB,
                         default={"temperature": 0.1, "top_p": 0.3, "frequency_penalty": 0.7, "presence_penalty": 0.4,
                                  "max_tokens": 512})
    prompt_type = Column(String, default="simple")
    prompt_config = Column(JSONB,
                           default={"system": "", "prologue": "您好，我是您的助手小樱，长得可爱又善良，can I help you?",
                                    "parameters": [], "empty_response": "Sorry! 知识库中未找到相关内容！"})
    similarity_threshold = Column(Float, default=0.2)
    vector_similarity_weight = Column(Float, default=0.3)
    top_n = Column(Integer, default=6)
    top_k = Column(Integer, default=1024)
    do_refer = Column(String, default="1")
    rerank_id = Column(String)
    kb_ids = Column(JSONB, default=[])
    status = Column(String, default="1")


class Conversation(BaseModel):
    __tablename__ = "t_ai_conversations"
    __table_args__ = {"schema": "test_dve"}  # 使用public schema

    id = Column(String, primary_key=True, index=True)
    dialog_id = Column(String, index=True)
    name = Column(String)
    message = Column(JSONB)
    reference = Column(JSONB, default=[])


class APIToken(BaseModel):
    __tablename__ = "t_ai_api_tokens"
    __table_args__ = {"schema": "test_dve"}  # 使用public schema

    tenant_id = Column(String, primary_key=True)
    token = Column(String, primary_key=True)
    dialog_id = Column(String)


class API4Conversation(BaseModel):
    __tablename__ = "t_ai_api4conversations"
    __table_args__ = {"schema": "test_dve"}

    id = Column(String, primary_key=True, index=True)
    dialog_id = Column(String, index=True)
    user_id = Column(String)
    message = Column(JSONB)
    reference = Column(JSONB, default=[])
    tokens = Column(Integer, default=0)
    duration = Column(Float, default=0)
    round = Column(Integer, default=0)
    thumb_up = Column(Integer, default=0)


class UserCanvas(BaseModel):
    __tablename__ = "t_ai_user_canvases"
    __table_args__ = {"schema": "test_dve"}

    id = Column(String, primary_key=True, index=True)
    avatar = Column(Text)
    user_id = Column(String)
    title = Column(String)
    description = Column(Text)
    canvas_type = Column(String)
    dsl = Column(JSONB, default={})


class CanvasTemplate(BaseModel):
    __tablename__ = "t_ai_canvas_templates"
    __table_args__ = {"schema": "test_dve"}

    id = Column(String, primary_key=True, index=True)
    avatar = Column(Text)
    title = Column(String)
    description = Column(Text)
    canvas_type = Column(String)
    dsl = Column(JSONB, default={})


def init_database_tables():
    # 需要创建的 schema 名称
    schema_name = 'test_dve'

    # 检查并创建 schema
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_name}"))
        connection.execute(text("COMMIT"))  # 提交创建schema的事务

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
