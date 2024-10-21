# llm_services.py
from sqlalchemy.orm import Session
from core.llm import ChatModel, CvModel, EmbeddingModel, Seq2txtModel, RerankModel, TTSModel

from api.db.services.user_service import TenantService
from api.settings import database_logger
from api.db import LLMType
from api.db.db_models import LLMFactories, LLM, TenantLLM
from api.db.services.common_service import CommonService


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
        print(f"Debug: Fetching API key for tenant_id={tenant_id}, model_name={model_name}")
        arr = model_name.split("@")
        if len(arr) < 2:
            objs = cls.query(db, tenant_id=tenant_id, llm_name=model_name)
        else:
            objs = cls.query(db, tenant_id=tenant_id, llm_name=arr[0], llm_factory=arr[1])
        if not objs:
            print("Debug: No API key found")
            return None
        print(f"Debug: Found API key: {objs[0].api_key}")
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

    @classmethod
    def model_instance(cls, db: Session, tenant_id: str, llm_type: str, llm_name: str = None, lang: str = "Chinese"):
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

        print(f"Debug: Fetching model instance for tenant_id={tenant_id}, mdlnm={mdlnm}")

        model_config = cls.get_api_key(db, tenant_id, mdlnm)
        # print("model_config:", model_config)

        tmp = mdlnm.split("@")
        fid = None if len(tmp) < 2 else tmp[1]
        mdlnm = tmp[0]

        if model_config:
            model_config = model_config.to_dict()
        else:
            print(f"Debug: No API key found for model {mdlnm}")

        if not model_config:
            print(f"Debug: Model({mdlnm}) not authorized")
            if llm_type in [LLMType.EMBEDDING, LLMType.RERANK]:
                llm = LLMService.query(db, llm_name=mdlnm) if not fid else LLMService.query(db, llm_name=mdlnm, fid=fid)
                if llm and llm[0].fid in ["Youdao", "FastEmbed", "BAAI"]:
                    model_config = {"llm_factory": llm[0].fid, "api_key": "",
                                    "llm_name": mdlnm, "api_base": ""}
            if not model_config:
                if mdlnm == "flag-embedding":
                    model_config = {"llm_factory": "Tongyi-Qianwen", "api_key": "", "llm_name": llm_name,
                                    "api_base": ""}
                else:
                    if not mdlnm:
                        raise LookupError(f"Type of {llm_type} model is not set.")
                    raise LookupError(f"Model({mdlnm}) not authorized")

        if llm_type == LLMType.EMBEDDING.value:
            if model_config["llm_factory"] not in EmbeddingModel:
                print(f"Debug: Embedding model factory not supported: {model_config['llm_factory']}")
                return None
            return EmbeddingModel[model_config["llm_factory"]](
                model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"]
            )

        if llm_type == LLMType.RERANK:
            if model_config["llm_factory"] not in RerankModel:
                return
            return RerankModel[model_config["llm_factory"]](
                model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"])

        if llm_type == LLMType.IMAGE2TEXT.value:
            if model_config["llm_factory"] not in CvModel:
                print(f"Debug: Image2Text model factory not supported: {model_config['llm_factory']}")
                return None
            return CvModel[model_config["llm_factory"]](
                model_config["api_key"], model_config["llm_name"], lang, base_url=model_config["api_base"]
            )

        if llm_type == LLMType.CHAT.value:
            if model_config["llm_factory"] not in ChatModel:
                print(f"Debug: Chat model factory not supported: {model_config['llm_factory']}")
                return None
            return ChatModel[model_config["llm_factory"]](
                model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"]
            )

        if llm_type == LLMType.SPEECH2TEXT:
            if model_config["llm_factory"] not in Seq2txtModel:
                return
            # return Seq2txtModel[model_config["llm_factory"]](
            #     model_config["api_key"], model_config["llm_name"], lang,
            #     base_url=model_config["api_base"]
            # )
            return Seq2txtModel[model_config["llm_factory"]](
                model_config["api_key"], model_config["llm_name"]
            )

        if llm_type == LLMType.TTS:
            if model_config["llm_factory"] not in TTSModel:
                return
            return TTSModel[model_config["llm_factory"]](
                model_config["api_key"],
                model_config["llm_name"],
                base_url=model_config["api_base"],
            )
    @classmethod
    def increase_usage(cls, db: Session, tenant_id: str, llm_type: str, used_tokens: int, llm_name: str = None):
        tenant = TenantService.get_by_id(db, tenant_id)
        if not tenant:
            raise LookupError("Tenant not found")

        if llm_type == LLMType.EMBEDDING.value:
            mdlnm = tenant.embd_id
        elif llm_type == LLMType.SPEECH2TEXT.value:
            mdlnm = tenant.asr_id
        elif llm_type == LLMType.IMAGE2TEXT.value:
            mdlnm = tenant.img2txt_id
        elif llm_type == LLMType.CHAT.value:
            mdlnm = tenant.llm_id if not llm_name else llm_name
        elif llm_type == LLMType.RERANK:
            mdlnm = tenant.rerank_id if not llm_name else llm_name
        elif llm_type == LLMType.TTS:
            mdlnm = tenant.tts_id if not llm_name else llm_name
        else:
            raise ValueError("LLM type error")

        llm_name = mdlnm.split("@")[0] if "@" in mdlnm else mdlnm

        num = 0
        try:
            for u in cls.query(db, tenant_id=tenant_id, llm_name=llm_name):
                num += cls.model.update({cls.model.used_tokens: u.used_tokens + used_tokens}).where(
                    cls.model.tenant_id == tenant_id, cls.model.llm_name == llm_name
                ).execute()
        except Exception as e:
            pass
        return num


    @classmethod
    def get_openai_models(cls, db: Session):
        objs = db.query(cls.model).filter(
            cls.model.llm_factory == "OpenAI",
            cls.model.llm_name.notin_(["text-embedding-3-small", "text-embedding-3-large"])
        ).all()
        return objs


class LLMBundle(object):
    def __init__(self, db: Session, tenant_id: str, llm_type: str, llm_name: str = None, lang: str = "Chinese"):
        self.db = db
        self.tenant_id = tenant_id
        self.llm_type = llm_type
        self.llm_name = llm_name
        self.lang = lang
        self.mdl = TenantLLMService.model_instance(db, tenant_id, llm_type, llm_name)
        if not self.mdl:
            raise ValueError(f"Can't find model for {tenant_id}/{llm_type}/{llm_name}")
        self.max_length = 8192
        for lm in LLMService.query(db, llm_name=llm_name):
            self.max_length = lm.max_tokens
            break

    def encode(self, texts: list, batch_size: int = 32):
        emd, used_tokens = self.mdl.encode(texts, batch_size)
        if not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, used_tokens):
            database_logger.error(f"Can't update token usage for {self.tenant_id}/EMBEDDING used_tokens: {used_tokens}")
        return emd, used_tokens

    def encode_queries(self, query: str):
        emd, used_tokens = self.mdl.encode_queries(query)
        if not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, used_tokens):
            database_logger.error(f"Can't update token usage for {self.tenant_id}/EMBEDDING used_tokens: {used_tokens}")
        return emd, used_tokens

    def similarity(self, query: str, texts: list):
        sim, used_tokens = self.mdl.similarity(query, texts)
        if not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, used_tokens):
            database_logger.error(f"Can't update token usage for {self.tenant_id}/RERANK used_tokens: {used_tokens}")
        return sim, used_tokens

    def describe(self, image, max_tokens: int = 300):
        txt, used_tokens = self.mdl.describe(image, max_tokens)
        if not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, used_tokens):
            database_logger.error(f"Can't update token usage for {self.tenant_id}/IMAGE2TEXT used_tokens: {used_tokens}")
        return txt

    def transcription(self, audio):
        txt, used_tokens = self.mdl.transcription(audio)
        if not TenantLLMService.increase_usage(
                self.db, self.tenant_id, self.llm_type, used_tokens):
            database_logger.error(
                "Can't update token usage for {}/SEQUENCE2TXT used_tokens: {}".format(self.tenant_id, used_tokens))
        return txt

    def tts(self, text):
        if not text.strip():
            return  # Skip processing if text is empty or whitespace
        try:
            for chunk in self.mdl.tts(text):
                if isinstance(chunk, int):
                    if not TenantLLMService.increase_usage(
                            self.db, self.tenant_id, self.llm_type, chunk, self.llm_name):
                        database_logger.error(
                            "Can't update token usage for {}/TTS".format(self.tenant_id))
                    return
                yield chunk
        except Exception as e:
            database_logger.error(f"TTS processing failed for text '{text}': {e}")

    def chat(self, system, history, gen_conf):
        txt, used_tokens = self.mdl.chat(system, history, gen_conf)
        if isinstance(txt, int) and  not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, used_tokens, self.llm_name):
            database_logger.error(f"Can't update token usage for {self.tenant_id}/CHAT used_tokens: {used_tokens}")
        return txt

    def chat_streamly(self, system, history, gen_conf):
        for txt in self.mdl.chat_streamly(system, history, gen_conf):
            if isinstance(txt, int):
                if not TenantLLMService.increase_usage(self.db, self.tenant_id, self.llm_type, txt, self.llm_name):
                    database_logger.error(f"Can't update token usage for {self.tenant_id}/CHAT llm_name: {self.llm_name}, content: {txt}")
                return
            yield txt
