import logging
import base64
import json
import os
import time
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session
from api.db import LLMType, UserTenantRole
from api.db.db_models import init_database_tables as init_web_db, LLM, LLMFactories, TenantLLM
from api.db.services import UserService
from api.db.services.canvas_service import CanvasTemplateService
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMFactoriesService, LLMService, TenantLLMService, LLMBundle
from api.db.services.user_service import TenantService, UserTenantService
from api import settings
from api.db.database import SessionLocal
from api.utils.file_utils import get_project_base_directory


def encode_to_base64(input_string):
    base64_encoded = base64.b64encode(input_string.encode('utf-8'))
    return base64_encoded.decode('utf-8')

def init_superuser(db: Session):
    user_info = {
        "id": uuid.uuid1().hex,
        "password": encode_to_base64("admin"),
        "nickname": "admin",
        "is_superuser": True,
        "email": "admin@datav.com",
        # "creator": "system",
        "status": "1",
    }
    tenant = {
        "id": user_info["id"],
        "name": user_info["nickname"] + "‘s Kingdom",
        "llm_id": settings.CHAT_MDL,
        "embd_id": settings.EMBEDDING_MDL,
        "asr_id": settings.ASR_MDL,
        "parser_ids": settings.PARSERS,
        "img2txt_id": settings.IMAGE2TEXT_MDL
    }
    usr_tenant = {
        "tenant_id": user_info["id"],
        "user_id": user_info["id"],
        "invited_by": user_info["id"],
        "role": UserTenantRole.OWNER
    }
    tenant_llm = []
    for llm in LLMService.query(db, fid=settings.LLM_FACTORY):
        tenant_llm.append(
            {
                "tenant_id": user_info["id"],
                "llm_factory": settings.LLM_FACTORY,
                "llm_name": llm.llm_name,
                "mdl_type": llm.mdl_type,
                "api_key": settings.API_KEY,
                "api_base": settings.LLM_BASE_URL
            }
        )
    # print(tenant_llm)
    if not UserService.save(db, **user_info):
        # print("\033[93m【ERROR】\033[0mcan't init admin.")
        logging.error("can't init admin.")
        return
    TenantService.insert(db, **tenant)
    UserTenantService.insert(db, **usr_tenant)
    TenantLLMService.insert_many(db, tenant_llm)
    # print(
    #     "【INFO】Super user initialized. \033[93memail: admin@datav.com, password: admin\033[0m. Changing the password after logging in is strongly recommended.")
    logging.info(
        "Super user initialized. email: admin@ragflow.io, password: admin. Changing the password after login is strongly recommended.")

    try:
        chat_mdl = LLMBundle(db, tenant["id"], LLMType.CHAT, tenant["llm_id"])
        # print(f"Model instance created for {tenant['llm_id']}")
        logging.info(f"Model instance created for {tenant['llm_id']}")
    except LookupError as e:
        # print(f"Error: {e}")
        logging.error(f"Error: {e}")
    msg = chat_mdl.chat(system="", history=[
        {"role": "user", "content": "Hello!"}], gen_conf={})
    if msg.find("ERROR: ") == 0:
        # print(
        #     "\33[91m【ERROR】\33[0m: ",
        #     "'{}' doesn't work. {}".format(
        #         tenant["llm_id"],
        #         msg))
        logging.error(
            "'{}' dosen't work. {}".format(
                tenant["llm_id"],
                msg))
    else:
        # print("【success！！！】" + msg)
        logging.info("【success！！！】" + msg)
    embd_mdl = LLMBundle(db, tenant["id"], LLMType.EMBEDDING, tenant["embd_id"])
    v, c = embd_mdl.encode(["Hello!"])
    if c == 0:
        # print(
        #     "\33[91m【ERROR】\33[0m:",
        #     " '{}' doesn't work!".format(
        #         tenant["embd_id"]))
        logging.error(
            "'{}' dosen't work!".format(
                tenant["embd_id"]))


def init_llm_factory(db: Session):
    try:
        LLMService.filter_delete(db, [(LLM.fid == "MiniMax" or LLM.fid == "Minimax")])
        LLMService.filter_delete(db, [(LLM.fid == "cohere")])
        LLMFactoriesService.filter_delete(db, [LLMFactories.name == "cohere"])
    except Exception as e:
        pass

    factory_llm_infos = json.load(
        open(
            os.path.join(get_project_base_directory(), "configs", "llm_factories.json"),
            "r",
        )
    )
    for factory_llm_info in factory_llm_infos["factory_llm_infos"]:
        llm_infos = factory_llm_info.pop("llm")
        if not llm_infos:
            # print(f"No LLM info for factory: {factory_llm_info['name']}")
            continue
        # print(f"Inserting LLM factory: {factory_llm_info['name']}")

        try:
            LLMFactoriesService.save(db, **factory_llm_info)
            # print(f"Saved LLM factory: {factory_llm_info['name']}")
        except Exception as e:
            # print(f"Error saving LLM factory {factory_llm_info['name']}: {e}")
            pass
        LLMService.filter_delete(db, [LLM.fid == factory_llm_info["name"]])
        for llm_info in llm_infos:
            llm_info["fid"] = factory_llm_info["name"]
            # print(llm_info)
            try:
                LLMService.save(db, **llm_info)
            except Exception as e:
                pass

    LLMFactoriesService.filter_delete(db, [LLMFactories.name == "Local"])
    LLMService.filter_delete(db, [LLM.fid == "Local"])
    LLMService.filter_delete(db, [LLM.llm_name == "qwen-vl-max"])
    LLMService.filter_delete(db, [LLM.fid == "Moonshot", LLM.llm_name == "flag-embedding"])
    TenantLLMService.filter_delete(db, [TenantLLM.llm_factory == "Moonshot", TenantLLM.llm_name == "flag-embedding"])
    LLMFactoriesService.filter_delete(db, [LLMFactories.name == "QAnything"])
    LLMService.filter_delete(db, [LLM.fid == "QAnything"])
    TenantLLMService.filter_update(db, [TenantLLM.llm_factory == "QAnything"], {"llm_factory": "Youdao"})
    TenantLLMService.filter_update(db, [TenantLLMService.model.llm_factory == "cohere"], {"llm_factory": "Cohere"})
    TenantService.filter_update(db, [1 == 1], {
        "parser_ids": "naive:General,qa:Q&A,resume:Resume,manual:Manual,table:Table,paper:Paper,book:Book,laws:Laws,presentation:Presentation,picture:Picture,one:One,audio:Audio,knowledge_graph:Knowledge Graph,email:Email"})
    # insert openai two embedding models to the current openai user.
    # print("Start to insert 2 OpenAI embedding models...")
    # tenant_ids = set([row.tenant_id for row in TenantLLMService.get_openai_models(db)])
    # for tid in tenant_ids:
    #     for row in TenantLLMService.query(db, llm_factory="OpenAI", tenant_id=tid):
    #         row = row.to_dict()
    #         row["mdl_type"] = LLMType.EMBEDDING.value
    #         row["llm_name"] = "text-embedding-3-small"
    #         row["used_tokens"] = 0
    #         try:
    #             TenantLLMService.save(db, **row)
    #             row = deepcopy(row)
    #             row["llm_name"] = "text-embedding-3-large"
    #             TenantLLMService.save(db, **row)
    #         except Exception as e:
    #             pass
    #         break
    for kb_id in KnowledgebaseService.get_all_ids(db):
        KnowledgebaseService.update_by_id(db, kb_id, {"doc_num": DocumentService.get_kb_doc_count(db, kb_id)})
    """
    DROP TABLE IF EXISTS t_ai_llm CASCADE;
    DROP TABLE IF EXISTS t_ai_llm_factories CASCADE;
    UPDATE t_ai_tenants
    SET parser_ids = 'naive:General,qa:Q&A,resume:Resume,manual:Manual,table:Table,paper:Paper,book:Book,laws:Laws,presentation:Presentation,picture:Picture,one:One,audio:Audio,knowledge_graph:Knowledge Graph,email:Email';
    ALTER TABLE t_ai_knowledgebases ALTER COLUMN avatar TYPE TEXT;
    ALTER TABLE t_ai_users ALTER COLUMN avatar TYPE TEXT;
    ALTER TABLE t_ai_dialogs ALTER COLUMN icon TYPE TEXT;
    """


def add_graph_templates(db: Session):
    dir = os.path.join(get_project_base_directory(), "agent", "templates")
    for fnm in os.listdir(dir):
        try:
            with open(os.path.join(dir, fnm), "r", encoding="utf-8") as f:
                cnvs = json.load(f)

            # 将 ID 转换为字符串以确保一致性
            cnvs["id"] = str(cnvs["id"])

            # 在插入前检查记录是否已存在
            existing_template = None
            try:
                existing_template = CanvasTemplateService.get_by_id(db, cnvs["id"])
            except HTTPException as e:
                if e.status_code == 404:
                    # 记录不存在，继续执行插入逻辑
                    print(f"Template {cnvs['id']} not found, will insert.")
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

        except Exception:
            # print("Add graph templates error: ", e)
            # print("------------", flush=True)
            logging.exception("Add graph templates error: ")

            db.rollback()  # 回滚事务


def init_web_data(db: Session = SessionLocal()):
    start_time = time.time()

    init_llm_factory(db)
    # print(len(UserService().get_all(db)))
    if len(UserService.get_all(db)) == 0:
        init_superuser(db)

    add_graph_templates(db)
    logging.info("init web data success:{}".format(time.time() - start_time))


if __name__ == '__main__':
    init_web_db()
    init_web_data()
