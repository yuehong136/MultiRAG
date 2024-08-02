# schemas.py
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime


class UserBase(BaseModel):
    id: str
    email: str


class UserCreate(UserBase):
    password: str


class User(UserBase):
    access_token: Optional[str] = None
    nickname: Optional[str] = None
    avatar: Optional[str] = None
    language: Optional[str] = "English"
    color_schema: Optional[str] = "Bright"
    timezone: Optional[str] = "UTC+8\tAsia/Shanghai"
    last_login_time: Optional[datetime] = None
    is_authenticated: Optional[bool] = True
    is_active: Optional[bool] = True
    is_anonymous: Optional[bool] = False
    login_channel: Optional[str] = None
    status: Optional[str] = "1"
    is_superuser: Optional[bool] = False

    class Config:
        from_attributes = True


class TenantBase(BaseModel):
    id: str
    name: Optional[str] = None


class Tenant(TenantBase):
    public_key: Optional[str] = None
    llm_id: Optional[str] = None
    embd_id: Optional[str] = None
    asr_id: Optional[str] = None
    img2txt_id: Optional[str] = None
    rerank_id: Optional[str] = None
    parser_ids: Optional[str] = None
    credit: Optional[int] = 512
    status: Optional[str] = "1"

    class Config:
        from_attributes = True


class UserTenantBase(BaseModel):
    id: str
    user_id: str
    tenant_id: str


class UserTenant(UserTenantBase):
    role: Optional[str] = None
    invited_by: Optional[str] = None
    status: Optional[str] = "1"

    class Config:
        from_attributes = True


class InvitationCodeBase(BaseModel):
    id: str
    code: str


class InvitationCode(InvitationCodeBase):
    visit_time: Optional[datetime] = None
    user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    status: Optional[str] = "1"

    class Config:
        from_attributes = True


class LLMBase(BaseModel):
    llm_name: str


class LLM(LLMBase):
    mdl_type: Optional[str] = None
    fid: Optional[str] = None
    max_tokens: Optional[int] = 0
    tags: Optional[str] = None
    status: Optional[str] = "1"

    class Config:
        from_attributes = True


class TenantLLMBase(BaseModel):
    tenant_id: str
    llm_factory: str
    llm_name: str


class TenantLLM(TenantLLMBase):
    mdl_type: Optional[str] = None
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    used_tokens: Optional[int] = 0

    class Config:
        from_attributes = True


class KnowledgebaseBase(BaseModel):
    id: str
    tenant_id: str
    name: str


class Knowledgebase(KnowledgebaseBase):
    avatar: Optional[str] = None
    language: Optional[str] = "English"
    description: Optional[str] = None
    embd_id: Optional[str] = None
    permission: Optional[str] = "me"
    created_by: Optional[str] = None
    doc_num: Optional[int] = 0
    token_num: Optional[int] = 0
    chunk_num: Optional[int] = 0
    similarity_threshold: Optional[float] = 0.2
    vector_similarity_weight: Optional[float] = 0.3
    parser_id: Optional[str] = None
    parser_config: Optional[Dict[str, Any]] = {"pages": [[1, 1000000]]}
    status: Optional[str] = "1"

    class Config:
        from_attributes = True


class DocumentBase(BaseModel):
    id: str
    kb_id: str
    name: str


class Document(DocumentBase):
    thumbnail: Optional[str] = None
    parser_id: Optional[str] = None
    parser_config: Optional[Dict[str, Any]] = {"pages": [[1, 1000000]]}
    source_type: Optional[str] = "local"
    type: Optional[str] = None
    created_by: Optional[str] = None
    location: Optional[str] = None
    size: Optional[int] = 0
    token_num: Optional[int] = 0
    chunk_num: Optional[int] = 0
    progress: Optional[float] = 0
    progress_msg: Optional[str] = ""
    process_begin_at: Optional[datetime] = None
    process_duation: Optional[float] = 0
    run: Optional[str] = "0"
    status: Optional[str] = "1"

    class Config:
        from_attributes = True


class FileBase(BaseModel):
    id: str
    parent_id: str
    tenant_id: str
    name: str


class File(FileBase):
    location: Optional[str] = None
    size: Optional[int] = 0
    type: Optional[str] = None
    source_type: Optional[str] = None

    class Config:
        from_attributes = True


class File2DocumentBase(BaseModel):
    id: str
    file_id: str
    document_id: str


class File2Document(File2DocumentBase):
    class Config:
        from_attributes = True


class TaskBase(BaseModel):
    id: str
    doc_id: str


class Task(TaskBase):
    from_page: Optional[int] = 0
    to_page: Optional[int] = -1
    begin_at: Optional[datetime] = None
    process_duation: Optional[float] = 0
    progress: Optional[float] = 0
    progress_msg: Optional[str] = ""

    class Config:
        from_attributes = True


class DialogBase(BaseModel):
    id: str
    tenant_id: str
    name: str


class Dialog(DialogBase):
    description: Optional[str] = None
    icon: Optional[str] = None
    language: Optional[str] = "English"
    llm_id: Optional[str] = None
    llm_setting: Optional[Dict[str, Any]] = {"temperature": 0.1, "top_p": 0.3, "frequency_penalty": 0.7,
                                             "presence_penalty": 0.4, "max_tokens": 512}
    prompt_type: Optional[str] = "simple"
    prompt_config: Optional[Dict[str, Any]] = {"system": "",
                                               "prologue": "您好，我是您的助手小樱，长得可爱又善良，can I help you?",
                                               "parameters": [], "empty_response": "Sorry! 知识库中未找到相关内容！"}
    similarity_threshold: Optional[float] = 0.2
    vector_similarity_weight: Optional[float] = 0.3
    top_n: Optional[int] = 6
    top_k: Optional[int] = 1024
    do_refer: Optional[str] = "1"
    rerank_id: Optional[str] = None
    kb_ids: Optional[List[str]] = []
    status: Optional[str] = "1"

    class Config:
        from_attributes = True


class ConversationBase(BaseModel):
    id: str
    dialog_id: str


class Conversation(ConversationBase):
    name: Optional[str] = None
    message: Optional[List[Dict[str, Any]]] = None
    reference: Optional[List[Dict[str, Any]]] = []

    class Config:
        from_attributes = True


class APITokenBase(BaseModel):
    tenant_id: str
    token: str
    dialog_id: str


class APIToken(APITokenBase):
    class Config:
        from_attributes = True


class API4ConversationBase(BaseModel):
    id: str
    dialog_id: str
    user_id: str


class API4Conversation(API4ConversationBase):
    message: Optional[List[Dict[str, Any]]] = None
    reference: Optional[List[Dict[str, Any]]] = []
    tokens: Optional[int] = 0
    duration: Optional[float] = 0
    round: Optional[int] = 0
    thumb_up: Optional[int] = 0

    class Config:
        from_attributes = True


class UserCanvasBase(BaseModel):
    id: str
    user_id: str


class UserCanvas(UserCanvasBase):
    avatar: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    canvas_type: Optional[str] = None
    dsl: Optional[Dict[str, Any]] = {}

    class Config:
        from_attributes = True


class CanvasTemplateBase(BaseModel):
    id: str


class CanvasTemplate(CanvasTemplateBase):
    avatar: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    canvas_type: Optional[str] = None
    dsl: Optional[Dict[str, Any]] = {}

    class Config:
        from_attributes = True
