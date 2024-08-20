# init.py

import json
import os
import time
import uuid
from copy import deepcopy
from sqlalchemy.orm import Session
from api.db import LLMType, UserTenantRole
from api.db.db_models import init_database_tables as init_web_db, LLM, LLMFactories, TenantLLM
from api.db.services import UserService
from api.db.services.canvas_service import CanvasTemplateService
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMFactoriesService, LLMService, TenantLLMService, LLMBundle
from api.db.services.user_service import TenantService, UserTenantService
from api.settings import CHAT_MDL, EMBEDDING_MDL, ASR_MDL, IMAGE2TEXT_MDL, PARSERS, LLM_FACTORY, API_KEY, LLM_BASE_URL
# from api.utils.file_utils import get_project_base_directory
from api.db.database import SessionLocal
from api.utils.file_utils import get_project_base_directory


def init_superuser(db: Session):
    user_info = {
        "id": uuid.uuid1().hex,
        "password": "admin",
        "nickname": "admin",
        "is_superuser": True,
        "email": "admin@datav.com",
        # "creator": "system",
        "status": "1",
    }
    tenant = {
        "id": user_info["id"],
        "name": user_info["nickname"] + "‘s Kingdom",
        "llm_id": CHAT_MDL,
        "embd_id": EMBEDDING_MDL,
        "asr_id": ASR_MDL,
        "parser_ids": PARSERS,
        "img2txt_id": IMAGE2TEXT_MDL
    }
    usr_tenant = {
        "tenant_id": user_info["id"],
        "user_id": user_info["id"],
        "invited_by": user_info["id"],
        "role": UserTenantRole.OWNER
    }
    tenant_llm = []
    for llm in LLMService.query(db, fid=LLM_FACTORY):
        tenant_llm.append(
            {
                "tenant_id": user_info["id"],
                "llm_factory": LLM_FACTORY,
                "llm_name": llm.llm_name,
                "mdl_type": llm.mdl_type,
                "api_key": API_KEY,
                "api_base": LLM_BASE_URL
            }
        )
    print(tenant_llm)
    if not UserService.save(db, **user_info):
        print("\033[93m【ERROR】\033[0mcan't init admin.")
        return
    TenantService.insert(db, **tenant)
    UserTenantService.insert(db, **usr_tenant)
    TenantLLMService.insert_many(db, tenant_llm)
    print(
        "【INFO】Super user initialized. \033[93memail: admin@datav.com, password: admin\033[0m. Changing the password after logging in is strongly recommended.")

    try:
        chat_mdl = LLMBundle(db, tenant["id"], LLMType.CHAT, tenant["llm_id"])
        print(f"Model instance created for {tenant['llm_id']}")
    except LookupError as e:
        print(f"Error: {e}")
    msg = chat_mdl.chat(system="", history=[
        {"role": "user", "content": "Hello!"}], gen_conf={})
    if msg.find("ERROR: ") == 0:
        print(
            "\33[91m【ERROR】\33[0m: ",
            "'{}' doesn't work. {}".format(
                tenant["llm_id"],
                msg))
    else:
        print("【success！！！】" + msg)
    # embd_mdl = LLMBundle(db, tenant["id"], LLMType.EMBEDDING, tenant["embd_id"])
    # v, c = embd_mdl.encode(db, ["Hello!"])
    # if c == 0:
    #     print(
    #         "\33[91m【ERROR】\33[0m:",
    #         " '{}' doesn't work!".format(
    #             tenant["embd_id"]))


def init_llm_factory(db: Session):
    try:
        LLMService.filter_delete(db, [(LLM.fid == "MiniMax" or LLM.fid == "Minimax")])
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

        for llm_info in llm_infos:
            llm_info["fid"] = factory_llm_info["name"]
            # print(llm_info)
            try:
                LLMService.save(db, **llm_info)
            except Exception as e:
                pass

    LLMFactoriesService.filter_delete(db, [LLMFactories.name == "Local"])
    LLMService.filter_delete(db, [LLM.fid == "Local"])
    LLMService.filter_delete(db, [LLM.fid == "Moonshot", LLM.llm_name == "flag-embedding"])
    TenantLLMService.filter_delete(db, [TenantLLM.llm_factory == "Moonshot", TenantLLM.llm_name == "flag-embedding"])
    LLMFactoriesService.filter_delete(db, [LLMFactories.name == "QAnything"])
    LLMService.filter_delete(db, [LLM.fid == "QAnything"])
    TenantLLMService.filter_update(db, [TenantLLM.llm_factory == "QAnything"], {"llm_factory": "Youdao"})
    TenantService.filter_update(db, [1 == 1], {
        "parser_ids": "naive:General,qa:Q&A,resume:Resume,manual:Manual,table:Table,paper:Paper,book:Book,laws:Laws,presentation:Presentation,picture:Picture,one:One,audio:Audio,knowledge_graph:Knowledge Graph,email:Email"})
    # insert openai two embedding models to the current openai user.
    print("Start to insert 2 OpenAI embedding models...")
    tenant_ids = set([row["tenant_id"] for row in TenantLLMService.get_openai_models(db)])
    for tid in tenant_ids:
        for row in TenantLLMService.query(db, llm_factory="OpenAI", tenant_id=tid):
            row = row.to_dict()
            row["mdl_type"] = LLMType.EMBEDDING.value
            row["llm_name"] = "text-embedding-3-small"
            row["used_tokens"] = 0
            try:
                TenantLLMService.save(db, **row)
                row = deepcopy(row)
                row["llm_name"] = "text-embedding-3-large"
                TenantLLMService.save(db, **row)
            except Exception as e:
                pass
            break
    for kb_id in KnowledgebaseService.get_all_ids(db):
        KnowledgebaseService.update_by_id(db, kb_id, {"doc_num": DocumentService.get_kb_doc_count(db, kb_id)})
    """
    drop table llm;
    drop table llm_factories;
    update tenant set parser_ids='naive:General,qa:Q&A,resume:Resume,manual:Manual,table:Table,paper:Paper,book:Book,laws:Laws,presentation:Presentation,picture:Picture,one:One,audio:Audio';
    alter table knowledgebase modify avatar longtext;
    alter table user modify avatar longtext;
    alter table dialog modify icon longtext;
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
            existing_template = CanvasTemplateService.get_by_id(db, cnvs["id"])

            if existing_template:
                # 如果记录存在，则更新
                try:
                    CanvasTemplateService.update_by_id(db, cnvs["id"], cnvs)
                except Exception as e:
                    print(f"Error updating template {cnvs['id']}: {e}")
                    db.rollback()  # 回滚事务
            else:
                # 如果记录不存在，则插入
                try:
                    CanvasTemplateService.save(db, **cnvs)
                except Exception as e:
                    print(f"Error saving template {cnvs['id']}: {e}")
                    db.rollback()  # 回滚事务

        except Exception as e:
            print("Add graph templates error: ", e)
            print("------------", flush=True)
            db.rollback()  # 回滚事务


def init_web_data(db: Session = SessionLocal()):
    start_time = time.time()

    init_llm_factory(db)
    # print(len(UserService().get_all(db)))
    if len(UserService.get_all(db)) == 0:
        init_superuser(db)

    add_graph_templates(db)
    print("init web data success:{}".format(time.time() - start_time))


if __name__ == '__main__':
    init_web_db()
    init_web_data()
