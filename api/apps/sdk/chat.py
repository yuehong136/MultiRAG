import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from common.constants import StatusEnum
from api.db.db_models import get_db
from api.db.services.dialog_service import DialogService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService
from common.misc_utils import get_uuid
from common.constants import RetCode
from api.utils.api_utils import check_duplicate_ids, get_error_data_result, get_result, token_required

router = APIRouter()


class CreateChatRequest(BaseModel):
    dataset_ids: list[str] | None = []
    llm: dict | None = None
    model_type: str | None = "chat"
    prompt: dict | None = None
    name: str
    description: str | None = "A helpful Assistant"
    avatar: str | None = ""
    top_n: int | None = 6
    top_k: int | None = 1024
    rerank_id: str | None = ""


class UpdateChatRequest(BaseModel):
    dataset_ids: list[str] | None = None
    llm: dict | None = None
    prompt: dict | None = None
    name: str | None = None
    description: str | None = None
    avatar: str | None = None
    top_n: int | None = None
    top_k: int | None = None
    rerank_id: str | None = None
    show_quotation: bool | None = None


class DeleteChatsRequest(BaseModel):
    ids: list[str] | None = None


@router.post("/chats", summary="创建聊天")
def create(request: CreateChatRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    req = request.model_dump()
    ids = [i for i in req.get("dataset_ids", []) if i]
    for kb_id in ids:
        kbs = KnowledgebaseService.accessible(db, kb_id=kb_id, user_id=tenant_id)
        if not kbs:
            return get_error_data_result(f"You don't own the dataset {kb_id}")
        kbs = KnowledgebaseService.query(db, id=kb_id)
        kb = kbs[0]
        if kb.chunk_num == 0:
            return get_error_data_result(f"The dataset {kb_id} doesn't own parsed file")

    kbs = KnowledgebaseService.get_by_ids(db, ids) if ids else []
    embd_ids = [TenantLLMService.split_model_name_and_factory(kb.embd_id)[0] for kb in kbs]  # remove vendor suffix for comparison
    embd_count = list(set(embd_ids))
    if len(embd_count) > 1:
        return get_result(retmsg='Datasets use different embedding models."', retcode=RetCode.AUTHENTICATION_ERROR)
    req["kb_ids"] = ids
    # llm
    llm = req.get("llm")
    if llm:
        if "model_name" in llm:
            req["llm_id"] = llm.pop("model_name")
            if req.get("llm_id") is not None:
                llm_name, llm_factory = TenantLLMService.split_model_name_and_factory(req["llm_id"])
                model_type = llm.get("model_type")
                model_type = model_type if model_type in ["chat", "image2text"] else "chat"
                if not TenantLLMService.query(db, tenant_id=tenant_id, llm_name=llm_name, llm_factory=llm_factory, mdl_type=model_type):
                    return get_error_data_result(f"`model_name` {req.get('llm_id')} doesn't exist")
        req["llm_setting"] = req.pop("llm")
    tenant = TenantService.get_by_id(db, tenant_id)
    if not tenant:
        return get_error_data_result(retmsg="Tenant not found!")
    # prompt
    prompt = req.get("prompt")
    key_mapping = {"parameters": "variables", "prologue": "opener", "quote": "show_quote", "system": "prompt", "rerank_id": "rerank_model", "vector_similarity_weight": "keywords_similarity_weight"}
    key_list = ["similarity_threshold", "vector_similarity_weight", "top_n", "rerank_id", "top_k"]
    if prompt:
        for new_key, old_key in key_mapping.items():
            if old_key in prompt:
                prompt[new_key] = prompt.pop(old_key)
        for key in key_list:
            if key in prompt:
                req[key] = prompt.pop(key)
        req["prompt_config"] = req.pop("prompt")
    # init
    req["id"] = get_uuid()
    req["description"] = req.get("description", "A helpful Assistant")
    req["icon"] = req.get("avatar", "")
    req["top_n"] = req.get("top_n", 6)
    req["top_k"] = req.get("top_k", 1024)
    req["rerank_id"] = req.get("rerank_id", "")
    if req.get("rerank_id"):
        value_rerank_model = ["BAAI/bge-reranker-v2-m3", "maidalun1020/bce-reranker-base_v1"]
        if req["rerank_id"] not in value_rerank_model and not TenantLLMService.query(db, tenant_id=tenant_id, llm_name=req.get("rerank_id"), mdl_type="rerank"):
            return get_error_data_result(f"`rerank_model` {req.get('rerank_id')} doesn't exist")
    if not req.get("llm_id"):
        req["llm_id"] = tenant.llm_id
    if not req.get("name"):
        return get_error_data_result(retmsg="`name` is required.")
    if DialogService.query(db, name=req["name"], tenant_id=tenant_id, status=StatusEnum.VALID.value):
        return get_error_data_result(retmsg="Duplicated chat name in creating chat.")
    # tenant_id
    if req.get("tenant_id"):
        return get_error_data_result(retmsg="`tenant_id` must not be provided.")
    req["tenant_id"] = tenant_id
    # prompt more parameter
    default_prompt = {
        "system": """You are an intelligent assistant. Please summarize the content of the dataset to answer the question. Please list the data in the dataset and answer in detail. When all dataset content is irrelevant to the question, your answer must include the sentence "The answer you are looking for is not found in the dataset!" Answers need to consider chat history.
      Here is the knowledge base:
      {knowledge}
      The above is the knowledge base.""",
        "prologue": "Hi! I'm your assistant. What can I do for you?",
        "parameters": [{"key": "knowledge", "optional": False}],
        "empty_response": "Sorry! No relevant content was found in the knowledge base!",
        "quote": True,
        "tts": False,
        "refine_multiturn": True,
    }
    key_list_2 = ["system", "prologue", "parameters", "empty_response", "quote", "tts", "refine_multiturn"]
    if "prompt_config" not in req:
        req["prompt_config"] = {}
    for key in key_list_2:
        temp = req["prompt_config"].get(key)
        if (not temp and key == "system") or (key not in req["prompt_config"]):
            req["prompt_config"][key] = default_prompt[key]
    for p in req["prompt_config"]["parameters"]:
        if p["optional"]:
            continue
        if req["prompt_config"]["system"].find("{%s}" % p["key"]) < 0:
            return get_error_data_result(retmsg="Parameter '{}' is not used".format(p["key"]))
    # save
    req.pop("dataset_ids")
    req.pop("avatar")
    if not DialogService.save(db, **req):
        return get_error_data_result(retmsg="Fail to new a chat!")
    # response
    res = DialogService.get_by_id(db, req["id"])
    if not res:
        return get_error_data_result(retmsg="Fail to new a chat!")
    res = res.to_dict()
    renamed_dict = {}
    for key, value in res["prompt_config"].items():
        new_key = key_mapping.get(key, key)
        renamed_dict[new_key] = value
    res["prompt"] = renamed_dict
    del res["prompt_config"]
    new_dict = {"similarity_threshold": res["similarity_threshold"], "keywords_similarity_weight": 1 - res["vector_similarity_weight"], "top_n": res["top_n"], "rerank_model": res["rerank_id"]}
    res["prompt"].update(new_dict)
    for key in key_list:
        del res[key]
    res["llm"] = res.pop("llm_setting")
    res["llm"]["model_name"] = res.pop("llm_id")
    res["dataset_ids"] = req.get("kb_ids", [])
    del res["kb_ids"]
    res["avatar"] = res.pop("icon")
    return get_result(data=res)


@router.put("/chats/{chat_id}", summary="更新聊天")
def update(chat_id: str, request: UpdateChatRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    if not DialogService.query(db, tenant_id=tenant_id, id=chat_id, status=StatusEnum.VALID.value):
        return get_error_data_result(retmsg="You do not own the chat")
    req = request.model_dump(exclude_unset=True)
    ids = req.get("dataset_ids", [])
    if "show_quotation" in req:
        req["do_refer"] = req.pop("show_quotation")
    if ids:
        for kb_id in ids:
            kbs = KnowledgebaseService.accessible(db, kb_id=kb_id, user_id=tenant_id)
            if not kbs:
                return get_error_data_result(f"You don't own the dataset {kb_id}")
            kbs = KnowledgebaseService.query(db, id=kb_id)
            kb = kbs[0]
            if kb.chunk_num == 0:
                return get_error_data_result(f"The dataset {kb_id} doesn't own parsed file")

        kbs = KnowledgebaseService.get_by_ids(db, ids)
        embd_ids = [TenantLLMService.split_model_name_and_factory(kb.embd_id)[0] for kb in kbs]  # remove vendor suffix for comparison
        embd_count = list(set(embd_ids))
        if len(embd_count) > 1:
            return get_result(retmsg='Datasets use different embedding models."', retcode=RetCode.AUTHENTICATION_ERROR)
        req["kb_ids"] = ids
    else:
        req["kb_ids"] = []
    llm = req.get("llm")
    if llm:
        if "model_name" in llm:
            req["llm_id"] = llm.pop("model_name")
            if req.get("llm_id") is not None:
                llm_name, llm_factory = TenantLLMService.split_model_name_and_factory(req["llm_id"])
                model_type = llm.get("model_type")
                model_type = model_type if model_type in ["chat", "image2text"] else "chat"
                if not TenantLLMService.query(db, tenant_id=tenant_id, llm_name=llm_name, llm_factory=llm_factory, mdl_type=model_type):
                    return get_error_data_result(f"`model_name` {req.get('llm_id')} doesn't exist")
        req["llm_setting"] = req.pop("llm")
    tenant = TenantService.get_by_id(db, tenant_id)
    if not tenant:
        return get_error_data_result(retmsg="Tenant not found!")
    # prompt
    prompt = req.get("prompt")
    key_mapping = {"parameters": "variables", "prologue": "opener", "quote": "show_quote", "system": "prompt", "rerank_id": "rerank_model", "vector_similarity_weight": "keywords_similarity_weight"}
    key_list = ["similarity_threshold", "vector_similarity_weight", "top_n", "rerank_id", "top_k"]
    if prompt:
        for new_key, old_key in key_mapping.items():
            if old_key in prompt:
                prompt[new_key] = prompt.pop(old_key)
        for key in key_list:
            if key in prompt:
                req[key] = prompt.pop(key)
        req["prompt_config"] = req.pop("prompt")
    res = DialogService.get_by_id(db, chat_id)
    if not res:
        return get_error_data_result(retmsg="Chat not found!")
    res = res.to_dict()
    if req.get("rerank_id"):
        value_rerank_model = ["BAAI/bge-reranker-v2-m3", "maidalun1020/bce-reranker-base_v1"]
        if req["rerank_id"] not in value_rerank_model and not TenantLLMService.query(db, tenant_id=tenant_id, llm_name=req.get("rerank_id"), mdl_type="rerank"):
            return get_error_data_result(f"`rerank_model` {req.get('rerank_id')} doesn't exist")
    if "name" in req:
        if not req.get("name"):
            return get_error_data_result(retmsg="`name` cannot be empty.")
        if req["name"].lower() != res["name"].lower() and len(DialogService.query(db, name=req["name"], tenant_id=tenant_id, status=StatusEnum.VALID.value)) > 0:
            return get_error_data_result(retmsg="Duplicated chat name in updating chat.")
    if "prompt_config" in req:
        res["prompt_config"].update(req["prompt_config"])
        for p in res["prompt_config"]["parameters"]:
            if p["optional"]:
                continue
            if res["prompt_config"]["system"].find("{%s}" % p["key"]) < 0:
                return get_error_data_result(retmsg="Parameter '{}' is not used".format(p["key"]))
    if "llm_setting" in req:
        res["llm_setting"].update(req["llm_setting"])
    req["prompt_config"] = res["prompt_config"]
    req["llm_setting"] = res["llm_setting"]
    # avatar
    if "avatar" in req:
        req["icon"] = req.pop("avatar")
    if "dataset_ids" in req:
        req.pop("dataset_ids")
    if not DialogService.update_by_id(db, chat_id, req):
        return get_error_data_result(retmsg="Chat not found!")
    return get_result()


@router.delete("/chats", summary="删除聊天")
def delete(request: DeleteChatsRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    errors = []
    success_count = 0
    req = request.model_dump()
    ids = req.get("ids")
    if not ids:
        id_list = []
        dias = DialogService.query(db, tenant_id=tenant_id, status=StatusEnum.VALID.value)
        for dia in dias:
            id_list.append(dia.id)
    else:
        id_list = ids

    unique_id_list, duplicate_messages = check_duplicate_ids(id_list, "assistant")

    for id in unique_id_list:
        if not DialogService.query(db, tenant_id=tenant_id, id=id, status=StatusEnum.VALID.value):
            errors.append(f"Assistant({id}) not found.")
            continue
        temp_dict = {"status": StatusEnum.INVALID.value}
        DialogService.update_by_id(db, id, temp_dict)
        success_count += 1

    if errors:
        if success_count > 0:
            return get_result(data={"success_count": success_count, "errors": errors}, retmsg=f"Partially deleted {success_count} chats with {len(errors)} errors")
        else:
            return get_error_data_result(retmsg="; ".join(errors))

    if duplicate_messages:
        if success_count > 0:
            return get_result(retmsg=f"Partially deleted {success_count} chats with {len(duplicate_messages)} errors", data={"success_count": success_count, "errors": duplicate_messages})
        else:
            return get_error_data_result(retmsg=";".join(duplicate_messages))

    return get_result()


@router.get("/chats", summary="获取聊天列表")
def list_chat(
    id: str | None = Query(None),
    name: str | None = Query(None),
    page: int = Query(1),
    page_size: int = Query(30),
    orderby: str = Query("create_time"),
    desc: bool = Query(True),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    if id or name:
        chat = DialogService.query(db, id=id, name=name, status=StatusEnum.VALID.value, tenant_id=tenant_id)
        if not chat:
            return get_error_data_result(retmsg="The chat doesn't exist")
    page_number = int(page)
    items_per_page = int(page_size)
    chats = DialogService.get_list(db, tenant_id, page_number, items_per_page, orderby, desc, id, name)
    if not chats:
        return get_result(data=[])
    list_assistants = []
    key_mapping = {
        "parameters": "variables",
        "prologue": "opener",
        "quote": "show_quote",
        "system": "prompt",
        "rerank_id": "rerank_model",
        "vector_similarity_weight": "keywords_similarity_weight",
        "do_refer": "show_quotation",
    }
    key_list = ["similarity_threshold", "vector_similarity_weight", "top_n", "rerank_id"]
    for res in chats:
        renamed_dict = {}
        for key, value in res["prompt_config"].items():
            new_key = key_mapping.get(key, key)
            renamed_dict[new_key] = value
        res["prompt"] = renamed_dict
        del res["prompt_config"]
        new_dict = {"similarity_threshold": res["similarity_threshold"], "keywords_similarity_weight": 1 - res["vector_similarity_weight"], "top_n": res["top_n"], "rerank_model": res["rerank_id"]}
        res["prompt"].update(new_dict)
        for key in key_list:
            del res[key]
        res["llm"] = res.pop("llm_setting")
        res["llm"]["model_name"] = res.pop("llm_id")
        kb_list = []
        for kb_id in res["kb_ids"]:
            kb = KnowledgebaseService.query(db, id=kb_id)
            if not kb:
                logging.warning(f"The kb {kb_id} does not exist.")
                continue
            kb_list.append(kb[0].to_dict())
        del res["kb_ids"]
        res["datasets"] = kb_list
        res["avatar"] = res.pop("icon")
        list_assistants.append(res)
    return get_result(data=list_assistants)
