import logging
import re
from functools import partial
from typing import Generator, Any

from langfuse import Langfuse

from sqlalchemy import update, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api.db.db_models import db_connection  # 导入上下文管理器
from api import settings
from api.db import LLMType
from api.db.db_models import LLMFactories, LLM, TenantLLM
from api.db.services.common_service import CommonService
from api.db.services.langfuse_service import TenantLangfuseService
from api.db.services.user_service import TenantService
from core.llm import ChatModel, CvModel, EmbeddingModel, Seq2txtModel, RerankModel, TTSModel


class LLMFactoriesService(CommonService):
    model = LLMFactories
    def __init__(self):
        super().__init__(LLMFactories)


class LLMService(CommonService):
    model = LLM
    def __init__(self):
        super().__init__(LLM)


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
            # model_factories = json.load(open(os.path.join(get_project_base_directory(), "configs/llm_factories.json"), "r"))["factory_llm_infos"]
            model_factories = settings.FACTORY_LLM_INFOS
            model_factories = set([f["name"] for f in model_factories])
            if arr[-1] not in model_factories:
                return model_name, None
            return arr[0], arr[-1]
        except Exception as e:
            logging.exception(f"TenantLLMService.split_model_name_and_factory got exception: {e}")
        return model_name, None

    @classmethod
    def get_model_config(cls, db: Session, tenant_id, llm_type, llm_name=None):
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
        # # 检查数据库连接状态并在需要时重新连接
        # try:
        #     # 尝试执行简单查询来测试连接
        #     db.execute(text("SELECT 1"))
        # except Exception:
        #     logging.warning("Database connection appears to be dead, attempting to reconnect...")
        #     try:
        #         # 回滚任何挂起的事务
        #         db.rollback()
        #         # 关闭当前连接
        #         db.close()
        #         # SQLAlchemy会在下次操作时自动重新连接
        #     except Exception:
        #         logging.exception("Failed to reset database connection")
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

        try:
            # 简化更新逻辑 - 直接更新增加的令牌数
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
            num = result.rowcount

            # 如果没有更新任何行，创建新记录
            if num == 0:
                if not llm_factory:
                    llm_factory = mdlnm
                new_tenant_llm = cls.model(
                    tenant_id=tenant_id,
                    mdl_type=llm_type,
                    llm_factory=llm_factory,
                    llm_name=llm_name,
                    used_tokens=used_tokens
                )
                db.add(new_tenant_llm)
                db.commit()
                num = 1

        except SQLAlchemyError:
            db.rollback()
            logging.exception(
                "TenantLLMService.increase_usage 出现异常，为tenant_id=%s, llm_name=%s更新used_tokens失败",
                tenant_id, llm_name
            )
            return 0

        return num

    @classmethod
    def get_openai_models(cls, db: Session):
        objs = db.query(cls.model).filter(cls.model.llm_factory == "OpenAI", cls.model.llm_name.notin_(["text-embedding-3-small", "text-embedding-3-large"])).all()
        return objs

    @staticmethod
    def llm_id2llm_type(llm_id: str) -> str | None:
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

# class LLMBundle(object):
#     def __init__(self, tenant_id: str, llm_type: str, llm_name: str = None, lang: str = "Chinese"):
#         self.tenant_id = tenant_id
#         self.llm_type = llm_type
#         self.llm_name = llm_name
#         self.lang = lang
#         # 使用上下文管理器初始化
#         with db_connection() as db:
#             self.mdl = TenantLLMService.model_instance(db, tenant_id, llm_type, llm_name)
#             if not self.mdl:
#                 raise ValueError(f"Can't find model for {tenant_id}/{llm_type}/{llm_name}")
#
#             self.max_length = 8192
#             for lm in LLMService.query(db, llm_name=llm_name):
#                 self.max_length = lm.max_tokens
#                 break
#
#     def encode(self, texts: list):
#         embeddings, used_tokens = self.mdl.encode(texts)
#         with db_connection() as db:
#             if not TenantLLMService.increase_usage(db, self.tenant_id, self.llm_type, used_tokens):
#                 logging.error(f"Can't update token usage for {self.tenant_id}/EMBEDDING used_tokens: {used_tokens}")
#         return embeddings, used_tokens
#
#     def encode_queries(self, query: str):
#         emd, used_tokens = self.mdl.encode_queries(query)
#         with db_connection() as db:
#             if not TenantLLMService.increase_usage(db, self.tenant_id, self.llm_type, used_tokens):
#                 logging.error(f"Can't update token usage for {self.tenant_id}/EMBEDDING used_tokens: {used_tokens}")
#         return emd, used_tokens
#
#     def similarity(self, query: str, texts: list):
#         sim, used_tokens = self.mdl.similarity(query, texts)
#         with db_connection() as db:
#             if not TenantLLMService.increase_usage(db, self.tenant_id, self.llm_type, used_tokens):
#                 logging.error(f"Can't update token usage for {self.tenant_id}/RERANK used_tokens: {used_tokens}")
#         return sim, used_tokens
#
#     def describe(self, image, max_tokens: int = 300):
#         txt, used_tokens = self.mdl.describe(image, max_tokens)
#         with db_connection() as db:
#             if not TenantLLMService.increase_usage(db, self.tenant_id, self.llm_type, used_tokens):
#                 logging.error(f"Can't update token usage for {self.tenant_id}/IMAGE2TEXT used_tokens: {used_tokens}")
#         return txt
#
#     def transcription(self, audio):
#         txt, used_tokens = self.mdl.transcription(audio)
#         with db_connection() as db:
#             if not TenantLLMService.increase_usage(
#                     db, self.tenant_id, self.llm_type, used_tokens):
#                 logging.error(
#                     "Can't update token usage for {}/SEQUENCE2TXT used_tokens: {}".format(self.tenant_id, used_tokens))
#         return txt
#
#     def tts(self, text):
#         if not text.strip():
#             return  # Skip processing if text is empty or whitespace
#         try:
#             for chunk in self.mdl.tts(text):
#                 if isinstance(chunk, int):
#                     with db_connection() as db:
#                         if not TenantLLMService.increase_usage(
#                                 db, self.tenant_id, self.llm_type, chunk, self.llm_name):
#                             logging.error(
#                                 "Can't update token usage for {}/TTS".format(self.tenant_id))
#                     return
#                 yield chunk
#         except Exception as e:
#             logging.error(f"TTS processing failed for text '{text}': {e}")
#
#     def chat(self, system, history, gen_conf, **kwargs):
#         txt, used_tokens = self.mdl.chat(system, history, gen_conf, **kwargs)
#         if not isinstance(txt, int):
#             with db_connection() as db:
#                 if not TenantLLMService.increase_usage(db, self.tenant_id, self.llm_type, used_tokens, self.llm_name):
#                     logging.error(f"Can't update token usage for {self.tenant_id}/CHAT used_tokens: {used_tokens}")
#         return txt
#
#     def chat_streamly(self, system, history, gen_conf, **kwargs):
#         for txt in self.mdl.chat_streamly(system, history, gen_conf, **kwargs):
#             if isinstance(txt, int):
#                 with db_connection() as db:
#                     if not TenantLLMService.increase_usage(db, self.tenant_id, self.llm_type, txt, self.llm_name):
#                         logging.error(f"Can't update token usage for {self.tenant_id}/CHAT llm_name: {self.llm_name}, content: {txt}")
#                 return
#             yield txt
class LLMBundle:
    def __init__(self, db: Session, tenant_id: str, llm_type: str, llm_name: str | None = None, lang: str = "Chinese", **kwargs):
        self.db = db
        self.tenant_id = tenant_id
        self.llm_type = llm_type
        self.llm_name = llm_name
        self.lang = lang
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

    def bind_tools(self, toolcall_session, tools):
        if not self.is_tools:
            logging.warning(f"Model {self.llm_name} does not support tool call, but you have assigned one or more tools to it!")
            return
        self.mdl.bind_tools(toolcall_session, tools)

    def encode(self, texts: list):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="encode", model=self.llm_name, input={"texts": texts})

        embeddings, used_tokens = self.mdl.encode(texts)
        llm_name = getattr(self, "llm_name", None)
        if not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, used_tokens, llm_name):
            logging.error(f"Can't update token usage for {self.tenant_id}/EMBEDDING used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(usage_details={"total_tokens": used_tokens})
            generation.end()

        return embeddings, used_tokens

    def encode_queries(self, query: str):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="encode_queries", model=self.llm_name, input={"query": query})

        emd, used_tokens = self.mdl.encode_queries(query)
        llm_name = getattr(self, "llm_name", None)
        if not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, used_tokens, llm_name):
            logging.error(f"Can't update token usage for {self.tenant_id}/EMBEDDING used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(usage_details={"total_tokens": used_tokens})
            generation.end()

        return emd, used_tokens

    def similarity(self, query: str, texts: list):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="similarity", model=self.llm_name, input={"query": query, "texts": texts})

        sim, used_tokens = self.mdl.similarity(query, texts)
        if not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, used_tokens):
            logging.error(f"Can't update token usage for {self.tenant_id}/RERANK used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(usage_details={"total_tokens": used_tokens})
            generation.end()

        return sim, used_tokens

    def describe(self, image, max_tokens: int = 300):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="describe", metadata={"model": self.llm_name})

        txt, used_tokens = self.mdl.describe(image)
        if not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, used_tokens):
            logging.error(f"Can't update token usage for {self.tenant_id}/IMAGE2TEXT used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})
            generation.end()

        return txt

    def describe_with_prompt(self, image, prompt):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="describe_with_prompt", metadata={"model": self.llm_name, "prompt": prompt})

        txt, used_tokens = self.mdl.describe_with_prompt(image, prompt)
        if not TenantLLMService.increase_usage(
                self.db, self.tenant_id, self.llm_type, used_tokens):
            logging.error("LLMBundle.describe can't update token usage for {}/IMAGE2TEXT used_tokens: {}".format(self.tenant_id, used_tokens))

        if self.langfuse:
            generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})
            generation.end()

        return txt

    def transcription(self, audio):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="transcription", metadata={"model": self.llm_name})

        txt, used_tokens = self.mdl.transcription(audio)
        if not TenantLLMService.increase_usage(
                self.db, self.tenant_id, self.llm_type, used_tokens):
            logging.error("Can't update token usage for {}/SEQUENCE2TXT used_tokens: {}".format(self.tenant_id, used_tokens))

        if self.langfuse:
            generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})
            generation.end()

        return txt

    def tts(self, text: str) -> Generator[bytes, None, None]:
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="tts", input={"text": text})

        for chunk in self.mdl.tts(text):
            if isinstance(chunk, int):
                if not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, chunk, self.llm_name):
                    logging.error("LLMBundle.tts can't update token usage for {}/TTS".format(self.tenant_id))
                return
            yield chunk

        if self.langfuse:
            generation.end()

    def _remove_reasoning_content(self, txt: str) -> str:
        first_think_start = txt.find("<think>")
        if first_think_start == -1:
            return txt

        last_think_end = txt.rfind("</think>")
        if last_think_end == -1:
            return txt

        if last_think_end < first_think_start:
            return txt

        return txt[last_think_end + len("</think>") :]

    def chat(self, system: str, history: list, gen_conf: dict[str, Any] | None=None, **kwargs) -> str:
        if gen_conf is None:
            gen_conf = {}
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="chat", model=self.llm_name, input={"system": system, "history": history})

        chat_partial = partial(self.mdl.chat, system, history, gen_conf)
        if self.is_tools and self.mdl.is_tools:
            chat_partial = partial(self.mdl.chat_with_tools, system, history, gen_conf)

        txt, used_tokens = chat_partial(**kwargs)
        txt = self._remove_reasoning_content(txt)

        if not self.verbose_tool_use:
            txt = re.sub(r"<tool_call>.*?</tool_call>", "", txt, flags=re.DOTALL)

        if isinstance(used_tokens, int) and not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, used_tokens, self.llm_name):
            logging.error("LLMBundle.chat can't update token usage for {}/CHAT llm_name: {}, used_tokens: {}".format(self.tenant_id, self.llm_name, used_tokens))

        if self.langfuse:
            generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})
            generation.end()

        return txt

    def chat_streamly(self, system: str, history: list, gen_conf:  dict[str, Any] | None=None, **kwargs):
        if gen_conf is None:
            gen_conf = {}
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="chat_streamly", model=self.llm_name, input={"system": system, "history": history})

        ans = ""
        chat_partial = partial(self.mdl.chat_streamly, system, history, gen_conf)
        total_tokens = 0
        if self.is_tools and self.mdl.is_tools:
            chat_partial = partial(self.mdl.chat_streamly_with_tools, system, history, gen_conf)

        for txt in chat_partial(**kwargs):
            if isinstance(txt, int):
                total_tokens = txt
                if self.langfuse:
                    generation.update(output={"output": ans})
                    generation.end()
                break

                # if not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, txt, self.llm_name):
                #     logging.error("LLMBundle.chat_streamly can't update token usage for {}/CHAT llm_name: {}, content: {}".format(self.tenant_id, self.llm_name, txt))
                # return ans

            if txt.endswith("</think>"):
                ans = ans.rstrip("</think>")

            if not self.verbose_tool_use:
                txt = re.sub(r"<tool_call>.*?</tool_call>", "", txt, flags=re.DOTALL)

            # if type(self.mdl).__name__ == "QWenCV":
            #     ans = txt
            # else:
            #     ans += txt
            ans += txt
            yield ans

        if total_tokens > 0:
            if not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, txt, self.llm_name):
                logging.error("LLMBundle.chat_streamly can't update token usage for {}/CHAT llm_name: {}, content: {}".format(self.tenant_id, self.llm_name, txt))