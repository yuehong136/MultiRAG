#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import logging
from langfuse import Langfuse
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api import settings
from api.db import LLMType
from api.db.db_models import LLMFactories, TenantLLM, db_connection
from api.db.services.common_service import CommonService
from api.db.services.langfuse_service import TenantLangfuseService
from api.db.services.user_service import TenantService
from core.llm import ChatModel, CvModel, EmbeddingModel, RerankModel, Seq2txtModel, TTSModel


class LLMFactoriesService(CommonService):
    model = LLMFactories
    def __init__(self):
        super().__init__(TenantLLM)


class TenantLLMService(CommonService):
    model = TenantLLM
    def __init__(self):
        super().__init__(TenantLLM)

    @classmethod
    def get_api_key(cls, db: Session, tenant_id: str, model_name: str):
        mdlnm, fid = TenantLLMService.split_model_name_and_factory(model_name)
        if not fid:
            objs = cls.query(db, tenant_id=tenant_id, llm_name=mdlnm)
        else:
            objs = cls.query(db, tenant_id=tenant_id, llm_name=mdlnm, llm_factory=fid)

        if (not objs) and fid:
            if fid == "LocalAI":
                mdlnm += "___LocalAI"
            elif fid == "HuggingFace":
                mdlnm += "___HuggingFace"
            elif fid == "OpenAI-API-Compatible":
                mdlnm += "___OpenAI-API"
            elif fid == "VLLM":
                mdlnm += "___VLLM"
            objs = cls.query(db, tenant_id=tenant_id, llm_name=mdlnm, llm_factory=fid)

        if not objs:
            return None
        return objs[0]

    @classmethod
    def get_my_llms(cls, db: Session, tenant_id: str):
        fields = [
            TenantLLM.llm_factory,
            LLMFactories.logo,
            LLMFactories.tags,
            TenantLLM.mdl_type,
            TenantLLM.llm_name,
            TenantLLM.used_tokens
        ]
        objs = db.query(*fields).join(LLMFactories, TenantLLM.llm_factory == LLMFactories.name).filter(
            TenantLLM.tenant_id == tenant_id, TenantLLM.api_key.isnot(None)
        ).all()
        return list(objs)

    @staticmethod
    def split_model_name_and_factory(model_name):
        arr = model_name.split("@")
        if len(arr) < 2:
            return model_name, None
        if len(arr) > 2:
            return "@".join(arr[0:-1]), arr[-1]

        # model name must be xxx@yyy
        try:
            model_factories = settings.FACTORY_LLM_INFOS
            model_providers = set([f["name"] for f in model_factories])
            if arr[-1] not in model_providers:
                return model_name, None
            return arr[0], arr[-1]
        except Exception as e:
            logging.exception(f"TenantLLMService.split_model_name_and_factory got exception: {e}")
        return model_name, None

    @classmethod
    def get_model_config(cls, db: Session, tenant_id, llm_type, llm_name=None):
        from api.db.services.llm_service import LLMService
        tenant = TenantService.get_by_id(db, tenant_id)
        if not tenant:
            raise LookupError("Tenant not found")

        if llm_type == LLMType.EMBEDDING.value:
            mdlnm = tenant.embd_id if not llm_name else llm_name
        elif llm_type == LLMType.SPEECH2TEXT.value:
            mdlnm = tenant.asr_id
        elif llm_type == LLMType.IMAGE2TEXT.value:
            mdlnm = tenant.img2txt_id if not llm_name else llm_name
        elif llm_type == LLMType.CHAT.value:
            mdlnm = tenant.llm_id if not llm_name else llm_name
        elif llm_type == LLMType.RERANK:
            mdlnm = tenant.rerank_id if not llm_name else llm_name
        elif llm_type == LLMType.TTS:
            mdlnm = tenant.tts_id if not llm_name else llm_name
        else:
            raise ValueError("LLM type error")

        model_config = cls.get_api_key(db, tenant_id, mdlnm)
        mdlnm, fid = TenantLLMService.split_model_name_and_factory(mdlnm)
        if not model_config:  # for some cases seems fid mismatch
            model_config = cls.get_api_key(db, tenant_id, mdlnm)
        if model_config:
            model_config = model_config.to_dict()
            llm = LLMService.query(db, llm_name=mdlnm) if not fid else LLMService.query(db, llm_name=mdlnm, fid=fid)
            if not llm and fid: # for some cases seems fid mismatch
                llm = LLMService.query(db, llm_name=mdlnm)
            if llm:
                model_config["is_tools"] = llm[0].is_tools
        else:
            logging.info(f"Debug: No API key found for model {mdlnm}")

        if not model_config:
            logging.info(f"Debug: Model({mdlnm}) not authorized")
            if llm_type in [LLMType.EMBEDDING, LLMType.RERANK]:
                llm = LLMService.query(db, llm_name=mdlnm) if not fid else LLMService.query(db, llm_name=mdlnm, fid=fid)
                if llm and llm[0].fid in ["Youdao", "FastEmbed", "BAAI"]:
                    model_config = {"llm_factory": llm[0].fid, "api_key": "", "llm_name": mdlnm, "api_base": ""}
            if not model_config:
                if mdlnm == "flag-embedding":
                    model_config = {"llm_factory": "Tongyi-Qianwen", "api_key": "", "llm_name": llm_name, "api_base": ""}
                else:
                    if not mdlnm:
                        raise LookupError(f"Type of {llm_type} model is not set.")
                    raise LookupError(f"Model({mdlnm}) not authorized")
        return model_config

    @classmethod
    def model_instance(cls, db: Session, tenant_id, llm_type, llm_name=None, lang="Chinese", **kwargs):
        model_config = TenantLLMService.get_model_config(db, tenant_id, llm_type, llm_name)
        kwargs.update({"provider": model_config["llm_factory"]})
        if llm_type == LLMType.EMBEDDING.value:
            if model_config["llm_factory"] not in EmbeddingModel:
                logging.info(f"Debug: Embedding model factory not supported: {model_config['llm_factory']}")
                return
            return EmbeddingModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"])

        if llm_type == LLMType.RERANK:
            if model_config["llm_factory"] not in RerankModel:
                return
            return RerankModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"])

        if llm_type == LLMType.IMAGE2TEXT.value:
            if model_config["llm_factory"] not in CvModel:
                logging.info(f"Debug: Image2Text model factory not supported: {model_config['llm_factory']}")
                return
            return CvModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], lang, base_url=model_config["api_base"], **kwargs)

        if llm_type == LLMType.CHAT.value:
            if model_config["llm_factory"] not in ChatModel:
                logging.info(f"Debug: Chat model factory not supported: {model_config['llm_factory']}")
                return
            return ChatModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"], **kwargs)

        if llm_type == LLMType.SPEECH2TEXT:
            if model_config["llm_factory"] not in Seq2txtModel:
                return
            return Seq2txtModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"])

        if llm_type == LLMType.TTS:
            if model_config["llm_factory"] not in TTSModel:
                return
            return TTSModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"])

    @classmethod
    def increase_usage(cls, db: Session, tenant_id: str, llm_type: str, used_tokens: int, llm_name: str | None = None):
        """增加LLM使用量

        逻辑: 仅执行UPDATE操作,不创建新记录
        重试: 处理索引损坏等临时错误,最多重试3次
        """
        tenant = TenantService.get_by_id(db, tenant_id)
        if not tenant:
            logging.error(f"Tenant not found: {tenant_id}")
            return 0

        llm_map = {
            LLMType.EMBEDDING.value: tenant.embd_id if not llm_name else llm_name,
            LLMType.SPEECH2TEXT.value: tenant.asr_id,
            LLMType.IMAGE2TEXT.value: tenant.img2txt_id,
            LLMType.CHAT.value: tenant.llm_id if not llm_name else llm_name,
            LLMType.RERANK.value: tenant.rerank_id if not llm_name else llm_name,
            LLMType.TTS.value: tenant.tts_id if not llm_name else llm_name
        }

        mdlnm = llm_map.get(llm_type)
        if mdlnm is None:
            logging.error(f"LLM type error: {llm_type}")
            return 0

        llm_name, llm_factory = TenantLLMService.split_model_name_and_factory(mdlnm)

        # 重试机制: 处理索引损坏等临时错误
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # 执行UPDATE操作 (与参考代码逻辑一致)
                stmt = (
                    update(cls.model)
                    .where(
                        cls.model.tenant_id == tenant_id,
                        cls.model.llm_name == llm_name,
                        cls.model.llm_factory == llm_factory if llm_factory else True
                    )
                    .values(used_tokens=cls.model.used_tokens + used_tokens)
                )
                result = db.execute(stmt)
                db.commit()
                return result.rowcount

            except SQLAlchemyError as e:
                db.rollback()
                error_msg = str(e)

                # 索引损坏错误: 重试
                if "IndexCorrupted" in error_msg or "invalid duplicate tuple" in error_msg:
                    if attempt < max_retries - 1:
                        logging.warning(
                            f"索引损坏错误,正在重试 ({attempt + 1}/{max_retries}): "
                            f"tenant_id={tenant_id}, llm_name={llm_name}"
                        )
                        import time
                        time.sleep(0.1 * (2 ** attempt))  # 指数退避: 0.1s, 0.2s, 0.4s
                        continue
                    else:
                        logging.error(
                            f"索引损坏持续存在,需要数据库维护: "
                            f"tenant_id={tenant_id}, llm_name={llm_name}\n"
                            "PostgreSQL: REINDEX INDEX usr_ai.ix_usr_ai_t_ai_tenant_llms_api_key;\n"
                            "MySQL: REPAIR TABLE usr_ai.t_ai_tenant_llms;"
                        )
                        return 0

                # 其他错误: 记录日志并返回
                else:
                    logging.exception(
                        "TenantLLMService.increase_usage 出现异常，"
                        f"tenant_id={tenant_id}, llm_name={llm_name}"
                    )
                    return 0

        return 0

    @classmethod
    def get_openai_models(cls, db: Session):
        objs = db.query(cls.model).filter(cls.model.llm_factory == "OpenAI", cls.model.llm_name.notin_(["text-embedding-3-small", "text-embedding-3-large"])).all()
        return objs

    @staticmethod
    def llm_id2llm_type(llm_id: str) -> str | None:
        from api.db.services.llm_service import LLMService
        llm_id, *_ = TenantLLMService.split_model_name_and_factory(llm_id)
        llm_factories = settings.FACTORY_LLM_INFOS
        for llm_factory in llm_factories:
            for llm in llm_factory["llm"]:
                if llm_id == llm["llm_name"]:
                    return llm["mdl_type"].split(",")[-1]
        with db_connection() as db:
            for llm in LLMService.query(db, llm_name=llm_id):
                return llm.mdl_type
            llm = TenantLLMService.get_or_none(db, llm_name=llm_id)
            if llm:
                return llm.mdl_type
            for llm in TenantLLMService.query(db, llm_name=llm_id):
                return llm.mdl_type


class LLM4Tenant:
    def __init__(self, db: Session, tenant_id: str, llm_type: str, llm_name: str | None = None, lang: str = "Chinese", **kwargs):
        self.db = db
        self.tenant_id = tenant_id
        self.llm_type = llm_type
        self.llm_name = llm_name
        self.mdl = TenantLLMService.model_instance(db, tenant_id, llm_type, llm_name, lang=lang, **kwargs)
        assert self.mdl, "Can't find model for {}/{}/{}".format(tenant_id, llm_type, llm_name)
        model_config = TenantLLMService.get_model_config(db, tenant_id, llm_type, llm_name)
        self.max_length = model_config.get("max_tokens", 8192)

        self.is_tools = model_config.get("is_tools", False)
        self.verbose_tool_use = kwargs.get("verbose_tool_use")

        langfuse_keys = TenantLangfuseService.filter_by_tenant(db, tenant_id=tenant_id)
        self.langfuse = None
        if langfuse_keys:
            langfuse = Langfuse(public_key=langfuse_keys.public_key, secret_key=langfuse_keys.secret_key, host=langfuse_keys.host)
            if langfuse.auth_check():
                self.langfuse = langfuse
                trace_id = self.langfuse.create_trace_id()
                self.trace_context = {"trace_id": trace_id}