import asyncio
import json
import logging
import os
from dataclasses import dataclass

from langfuse import Langfuse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from api.db.db_models import LLMFactories, TenantLLM, async_session_factory, db_connection
from api.db.services.common_service import CommonService
from api.db.services.langfuse_service import TenantLangfuseService
from api.db.services.user_service import TenantService
from common import settings
from common.constants import MINERU_DEFAULT_CONFIG, MINERU_ENV_KEYS, PADDLEOCR_DEFAULT_CONFIG, PADDLEOCR_ENV_KEYS, LLMType
from core.llm import ChatModel, CvModel, EmbeddingModel, OcrModel, RerankModel, Seq2txtModel, TTSModel


class LLMFactoriesService(CommonService):
    model = LLMFactories

    def __init__(self):
        super().__init__(TenantLLM)


class TenantLLMService(CommonService):
    model = TenantLLM

    @dataclass(slots=True, frozen=True)
    class UsageIdentity:
        tenant_id: str
        llm_type: str
        model_name: str
        llm_factory: str | None
        llm_name_raw: str | None

    def __init__(self):
        super().__init__(TenantLLM)

    _FACTORY_SUFFIX: dict[str, str] = {
        "LocalAI": "___LocalAI",
        "HuggingFace": "___HuggingFace",
        "OpenAI-API-Compatible": "___OpenAI-API",
        "VLLM": "___VLLM",
    }

    @classmethod
    def get_api_key(
        cls,
        db: Session,
        tenant_id: str,
        model_name: str,
        model_type: str | LLMType | None = None,
    ):
        mdlnm, fid = TenantLLMService.split_model_name_and_factory(model_name)
        model_type_value = model_type.value if hasattr(model_type, "value") else model_type

        stmt = select(cls.model).where(cls.model.tenant_id == tenant_id, cls.model.llm_name == mdlnm)
        if model_type_value is not None:
            stmt = stmt.where(cls.model.mdl_type == model_type_value)
        if fid:
            stmt = stmt.where(cls.model.llm_factory == fid)

        obj = db.execute(stmt).scalars().first()

        if obj is None and fid and (suffix := cls._FACTORY_SUFFIX.get(fid)):
            stmt = select(cls.model).where(
                cls.model.tenant_id == tenant_id,
                cls.model.llm_name == mdlnm + suffix,
                cls.model.llm_factory == fid,
            )
            if model_type_value is not None:
                stmt = stmt.where(cls.model.mdl_type == model_type_value)
            obj = db.execute(stmt).scalars().first()

        return obj

    @classmethod
    def get_my_llms(cls, db: Session, tenant_id: str):
        stmt = (
            select(
                TenantLLM.id,
                TenantLLM.llm_factory,
                LLMFactories.logo,
                LLMFactories.tags,
                TenantLLM.mdl_type,
                TenantLLM.llm_name,
                TenantLLM.used_tokens,
                TenantLLM.status,
            )
            .join(LLMFactories, TenantLLM.llm_factory == LLMFactories.name)
            .where(
                TenantLLM.tenant_id == tenant_id,
                TenantLLM.api_key.isnot(None),
            )
        )
        return list(db.execute(stmt).all())

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
            model_providers = {f["name"] for f in model_factories}
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
            mdlnm = tenant.asr_id if not llm_name else llm_name
        elif llm_type == LLMType.IMAGE2TEXT.value:
            mdlnm = tenant.img2txt_id if not llm_name else llm_name
        elif llm_type == LLMType.CHAT.value:
            mdlnm = tenant.llm_id if not llm_name else llm_name
        elif llm_type == LLMType.RERANK.value:
            mdlnm = tenant.rerank_id if not llm_name else llm_name
        elif llm_type == LLMType.TTS.value:
            mdlnm = tenant.tts_id if not llm_name else llm_name
        elif llm_type == LLMType.OCR.value:
            if not llm_name:
                raise LookupError("OCR model name is required")
            mdlnm = llm_name
        else:
            raise ValueError("LLM type error")

        model_config = cls.get_api_key(db, tenant_id, mdlnm, llm_type)
        mdlnm, fid = TenantLLMService.split_model_name_and_factory(mdlnm)
        if not model_config:  # for some cases seems fid mismatch
            model_config = cls.get_api_key(db, tenant_id, mdlnm, llm_type)
        if model_config:
            model_config = model_config.to_dict()
        elif llm_type == LLMType.EMBEDDING.value and fid == "Builtin" and "tei-" in os.getenv("COMPOSE_PROFILES", "") and mdlnm == os.getenv("TEI_MODEL", ""):
            embedding_cfg = settings.EMBEDDING_CFG
            model_config = {"llm_factory": "Builtin", "api_key": embedding_cfg["api_key"], "llm_name": mdlnm, "api_base": embedding_cfg["base_url"]}
        else:
            raise LookupError(f"Model({mdlnm}@{fid}) not authorized")

        llm = LLMService.query(db, llm_name=mdlnm) if not fid else LLMService.query(db, llm_name=mdlnm, fid=fid)
        if not llm and fid:  # for some cases seems fid mismatch
            llm = LLMService.query(db, llm_name=mdlnm)
        if llm:
            model_config["is_tools"] = llm[0].is_tools
        return model_config

    @classmethod
    def model_instance(cls, db: Session, tenant_id: str, model_config: dict, lang="Chinese", **kwargs):
        if not model_config:
            raise LookupError("Model config is required")
        model_type = model_config.get("mdl_type") or model_config.get("model_type")
        if not model_type:
            raise ValueError("Model config is missing mdl_type/model_type")

        kwargs.update({"provider": model_config["llm_factory"]})
        if model_type == LLMType.EMBEDDING.value:
            if model_config["llm_factory"] not in EmbeddingModel:
                logging.info(f"Debug: Embedding model factory not supported: {model_config['llm_factory']}")
                return None
            return EmbeddingModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"])

        elif model_type == LLMType.RERANK.value:
            if model_config["llm_factory"] not in RerankModel:
                return None
            return RerankModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"])

        elif model_type == LLMType.IMAGE2TEXT.value:
            if model_config["llm_factory"] not in CvModel:
                logging.info(f"Debug: Image2Text model factory not supported: {model_config['llm_factory']}")
                return None
            return CvModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], lang, base_url=model_config["api_base"], **kwargs)

        elif model_type == LLMType.CHAT.value:
            if model_config["llm_factory"] not in ChatModel:
                logging.info(f"Debug: Chat model factory not supported: {model_config['llm_factory']}")
                return None
            return ChatModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"], **kwargs)

        elif model_type == LLMType.SPEECH2TEXT.value:
            if model_config["llm_factory"] not in Seq2txtModel:
                return None
            return Seq2txtModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"])

        elif model_type == LLMType.TTS.value:
            if model_config["llm_factory"] not in TTSModel:
                return None
            return TTSModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"])

        elif model_type == LLMType.OCR.value:
            if model_config["llm_factory"] not in OcrModel:
                return None
            return OcrModel[model_config["llm_factory"]](
                key=model_config["api_key"],
                model_name=model_config["llm_name"],
                base_url=model_config.get("api_base", ""),
                **kwargs,
            )

        return None

    @classmethod
    def resolve_usage_identity(
        cls,
        db: Session,
        tenant_id: str,
        llm_type: str,
        llm_name: str | None = None,
    ) -> "TenantLLMService.UsageIdentity | None":
        """解析用量统计所需的稳定模型身份。"""
        mdlnm: str | None
        if llm_name:
            mdlnm = llm_name
        else:
            tenant = TenantService.get_by_id(db, tenant_id)
            if not tenant:
                logging.error(f"Tenant not found: {tenant_id}")
                return None

            llm_map = {
                LLMType.EMBEDDING.value: tenant.embd_id,
                LLMType.SPEECH2TEXT.value: tenant.asr_id,
                LLMType.IMAGE2TEXT.value: tenant.img2txt_id,
                LLMType.CHAT.value: tenant.llm_id,
                LLMType.RERANK.value: tenant.rerank_id,
                LLMType.TTS.value: tenant.tts_id,
                LLMType.OCR.value: llm_name,
            }
            mdlnm = llm_map.get(llm_type)

        if not mdlnm:
            logging.error(f"LLM type error or empty model: llm_type={llm_type}, tenant_id={tenant_id}, llm_name={llm_name}")
            return None

        model_name, llm_factory = TenantLLMService.split_model_name_and_factory(mdlnm)
        if not model_name:
            logging.error(f"Resolved empty model name: llm_type={llm_type}, tenant_id={tenant_id}, llm_name={mdlnm}")
            return None

        return cls.UsageIdentity(
            tenant_id=tenant_id,
            llm_type=llm_type,
            model_name=model_name,
            llm_factory=llm_factory,
            llm_name_raw=mdlnm,
        )

    @classmethod
    def increase_usage(cls, db: Session | None, tenant_id: str, llm_type: str, used_tokens: int, llm_name: str | None = None):
        """增加LLM使用量

        逻辑: 仅执行UPDATE操作,不创建新记录
        重试: 处理索引损坏等临时错误,最多重试3次

        注意: 调用方传入的 db 仅保留兼容性，不再复用其事务状态
        """
        # 这里是"业务旁路指标"，必须 best-effort：任何 DB 波动都不应导致主链路 500
        try:
            if not isinstance(used_tokens, int) or used_tokens <= 0:
                return 0

            # 独立短连接执行旁路记账，避免污染调用方 session
            with db_connection() as usage_db:
                identity = cls.resolve_usage_identity(usage_db, tenant_id, llm_type, llm_name)
                if identity is None:
                    return 0
                return cls._do_increase_usage(usage_db, identity, used_tokens)

        except Exception as e:
            # 任何异常都不应向上抛出
            logging.error(f"TenantLLMService.increase_usage unexpected error (ignored), tenant_id={tenant_id}, llm_type={llm_type}, llm_name={llm_name}, used_tokens={used_tokens}: {e}")
            return 0

    @classmethod
    def increase_usage_by_identity(
        cls,
        identity: "TenantLLMService.UsageIdentity | None",
        used_tokens: int,
    ) -> int:
        """按已解析身份执行独立 session 记账。"""
        try:
            if identity is None or not isinstance(used_tokens, int) or used_tokens <= 0:
                return 0

            with db_connection() as usage_db:
                return cls._do_increase_usage(usage_db, identity, used_tokens)
        except Exception as e:
            logging.error(
                "TenantLLMService.increase_usage_by_identity unexpected error (ignored), "
                f"tenant_id={getattr(identity, 'tenant_id', None)}, "
                f"llm_type={getattr(identity, 'llm_type', None)}, "
                f"llm_name={getattr(identity, 'llm_name_raw', None)}, "
                f"used_tokens={used_tokens}: {e}"
            )
            return 0

    @classmethod
    def increase_usage_by_id(cls, tenant_model_id: int | None, used_tokens: int) -> int:
        try:
            if tenant_model_id is None or not isinstance(used_tokens, int) or used_tokens <= 0:
                return 0

            with db_connection() as usage_db:
                stmt = update(cls.model).where(cls.model.id == tenant_model_id).values(used_tokens=cls.model.used_tokens + used_tokens)
                result = usage_db.execute(stmt)
                usage_db.commit()
                return result.rowcount
        except Exception as e:
            logging.error(f"TenantLLMService.increase_usage_by_id unexpected error (ignored), id={tenant_model_id}, used_tokens={used_tokens}: {e}")
            return 0

    @classmethod
    async def increase_usage_by_id_async(cls, tenant_model_id: int | None, used_tokens: int) -> int:
        """increase_usage_by_id 的异步版:async 调用链(SSE 流式等)内记账不阻塞事件循环。

        与同步版同语义:独立短会话、best-effort 吞异常;非 PG 后端无异步引擎时
        退回线程池跑同步版。
        """
        try:
            if tenant_model_id is None or not isinstance(used_tokens, int) or used_tokens <= 0:
                return 0

            if async_session_factory is None:
                return await asyncio.to_thread(cls.increase_usage_by_id, tenant_model_id, used_tokens)

            async with async_session_factory() as usage_db:
                stmt = update(cls.model).where(cls.model.id == tenant_model_id).values(used_tokens=cls.model.used_tokens + used_tokens)
                result = await usage_db.execute(stmt)
                await usage_db.commit()
                return result.rowcount
        except Exception as e:
            logging.error(f"TenantLLMService.increase_usage_by_id_async unexpected error (ignored), id={tenant_model_id}, used_tokens={used_tokens}: {e}")
            return 0

    @classmethod
    def _do_increase_usage(
        cls,
        db: Session,
        identity: "TenantLLMService.UsageIdentity",
        used_tokens: int,
    ):
        """实际执行 token 用量更新的内部方法。"""
        # 重试机制: 处理索引损坏等临时错误
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # 执行 UPDATE（不创建新记录）
                stmt = (
                    update(cls.model)
                    .where(
                        cls.model.tenant_id == identity.tenant_id,
                        cls.model.llm_name == identity.model_name,
                        cls.model.llm_factory == identity.llm_factory if identity.llm_factory else True,
                    )
                    .values(used_tokens=cls.model.used_tokens + used_tokens)
                )
                result = db.execute(stmt)
                db.commit()
                return result.rowcount

            except SQLAlchemyError as e:
                try:
                    db.rollback()
                except Exception:
                    pass

                error_msg = str(e)
                if "IndexCorrupted" in error_msg or "invalid duplicate tuple" in error_msg:
                    if attempt < max_retries - 1:
                        logging.warning(f"索引损坏错误,正在重试 ({attempt + 1}/{max_retries}): tenant_id={identity.tenant_id}, llm_name={identity.model_name}")
                        import time

                        time.sleep(0.1 * (2**attempt))
                        continue

                    logging.error(
                        f"索引损坏持续存在,需要数据库维护: "
                        f"tenant_id={identity.tenant_id}, llm_name={identity.model_name}\n"
                        "PostgreSQL: REINDEX INDEX usr_ai.ix_usr_ai_t_ai_tenant_llms_api_key;\n"
                        "MySQL: REPAIR TABLE usr_ai.t_ai_tenant_llms;"
                    )
                    return 0

                # 其他错误：best-effort，不影响主链路
                logging.exception(f"TenantLLMService.increase_usage 记录用量失败（已忽略），tenant_id={identity.tenant_id}, llm_name={identity.model_name}")
                return 0

        return 0

    @classmethod
    def get_openai_models(cls, db: Session):
        objs = db.query(cls.model).filter(cls.model.llm_factory == "OpenAI", cls.model.llm_name.notin_(["text-embedding-3-small", "text-embedding-3-large"])).all()
        return objs

    @classmethod
    def delete_by_tenant_id(cls, db: Session, tenant_id: str) -> int:
        """根据tenant_id删除所有相关的LLM配置记录"""
        try:
            result = db.query(cls.model).filter(cls.model.tenant_id == tenant_id).delete(synchronize_session=False)
            db.commit()
            return result
        except Exception as e:
            db.rollback()
            logging.exception(f"Failed to delete tenant LLM records for tenant_id={tenant_id}")
            raise e

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

    @classmethod
    def _collect_mineru_env_config(cls) -> dict | None:
        cfg = MINERU_DEFAULT_CONFIG.copy()
        found = False
        for key in MINERU_ENV_KEYS:
            val = os.environ.get(key)
            if val:
                found = True
                cfg[key] = val
        return cfg if found else None

    @classmethod
    def ensure_mineru_from_env(cls, db: Session, tenant_id: str) -> str | None:
        """
        Ensure a MinerU OCR model exists for the tenant if env variables are present.
        Return the existing or newly created llm_name, or None if env not set.
        """
        cfg = cls._collect_mineru_env_config()
        if not cfg:
            return None

        saved_mineru_models = cls.query(db, tenant_id=tenant_id, llm_factory="MinerU", mdl_type=LLMType.OCR.value)

        def _parse_api_key(raw: str) -> dict:
            try:
                return json.loads(raw or "{}")
            except Exception:
                return {}

        for item in saved_mineru_models:
            api_cfg = _parse_api_key(item.api_key)
            normalized = {k: api_cfg.get(k, MINERU_DEFAULT_CONFIG.get(k)) for k in MINERU_ENV_KEYS}
            if normalized == cfg:
                return item.llm_name

        used_names = {item.llm_name for item in saved_mineru_models}
        idx = 1
        base_name = "mineru-from-env"
        while True:
            candidate = f"{base_name}-{idx}"
            if candidate in used_names:
                idx += 1
                continue

            try:
                cls.save(
                    db,
                    tenant_id=tenant_id,
                    llm_factory="MinerU",
                    llm_name=candidate,
                    mdl_type=LLMType.OCR.value,
                    api_key=json.dumps(cfg),
                    api_base="",
                    max_tokens=0,
                )
                return candidate
            except IntegrityError:
                logging.warning("MinerU env model %s already exists for tenant %s, retry with next name", candidate, tenant_id)
                db.rollback()
                used_names.add(candidate)
                idx += 1
                continue

    @classmethod
    def _collect_paddleocr_env_config(cls) -> dict | None:
        cfg = PADDLEOCR_DEFAULT_CONFIG.copy()
        found = False
        for key in PADDLEOCR_ENV_KEYS:
            val = os.environ.get(key)
            if val:
                found = True
                cfg[key] = val
        return cfg if found else None

    @classmethod
    def ensure_paddleocr_from_env(cls, db: Session, tenant_id: str) -> str | None:
        """
        Ensure a PaddleOCR model exists for the tenant if env variables are present.
        Return the existing or newly created llm_name, or None if env not set.
        """
        cfg = cls._collect_paddleocr_env_config()
        if not cfg:
            return None

        saved_paddleocr_models = cls.query(db, tenant_id=tenant_id, llm_factory="PaddleOCR", mdl_type=LLMType.OCR.value)

        def _parse_api_key(raw: str) -> dict:
            try:
                return json.loads(raw or "{}")
            except Exception:
                return {}

        for item in saved_paddleocr_models:
            api_cfg = _parse_api_key(item.api_key)
            normalized = {k: api_cfg.get(k, PADDLEOCR_DEFAULT_CONFIG.get(k)) for k in PADDLEOCR_ENV_KEYS}
            if normalized == cfg:
                return item.llm_name

        used_names = {item.llm_name for item in saved_paddleocr_models}
        idx = 1
        base_name = "paddleocr-from-env"
        while True:
            candidate = f"{base_name}-{idx}"
            if candidate in used_names:
                idx += 1
                continue

            try:
                cls.save(
                    db,
                    tenant_id=tenant_id,
                    llm_factory="PaddleOCR",
                    llm_name=candidate,
                    mdl_type=LLMType.OCR.value,
                    api_key=json.dumps(cfg),
                    api_base="",
                    max_tokens=0,
                )
                return candidate
            except IntegrityError:
                logging.warning("PaddleOCR env model %s already exists for tenant %s, retry with next name", candidate, tenant_id)
                db.rollback()
                used_names.add(candidate)
                idx += 1
                continue


class LLM4Tenant:
    def __init__(self, db: Session | None, tenant_id: str, model_config: dict, lang: str = "Chinese", **kwargs):
        self.db = db
        self.tenant_id = tenant_id
        self.model_config = model_config
        self.llm_type = model_config.get("mdl_type") or model_config.get("model_type")
        self.llm_name = model_config.get("llm_name")
        self.langfuse = None
        self.trace_context = {}
        self.verbose_tool_use = kwargs.get("verbose_tool_use")

        if db is None:
            with db_connection() as init_db:
                self._initialize(init_db, tenant_id, model_config, lang, **kwargs)
        else:
            self._initialize(db, tenant_id, model_config, lang, **kwargs)

    def _initialize(
        self,
        db: Session,
        tenant_id: str,
        model_config: dict,
        lang: str,
        **kwargs,
    ) -> None:
        self.mdl = TenantLLMService.model_instance(db, tenant_id, model_config, lang=lang, **kwargs)
        assert self.mdl, "Can't find model for {}/{}".format(tenant_id, model_config.get("llm_name"))
        self.max_length = model_config.get("max_tokens", 8192)
        self.is_tools = model_config.get("is_tools", False)

        langfuse_keys = TenantLangfuseService.filter_by_tenant(db, tenant_id=tenant_id)
        if langfuse_keys:
            langfuse = Langfuse(public_key=langfuse_keys.public_key, secret_key=langfuse_keys.secret_key, host=langfuse_keys.host)
            try:
                if langfuse.auth_check():
                    self.langfuse = langfuse
                    trace_id = self.langfuse.create_trace_id()
                    self.trace_context = {"trace_id": trace_id}
            except Exception:
                # Skip langfuse tracing if connection fails
                pass

    def _release_db_before_long_io(self) -> None:
        """
        在进行外部（可能长耗时）调用前，尽量避免持有“只读事务”。

        关键点：
        - SQLAlchemy Session 只要做过一次查询，就会开启事务（哪怕是 SELECT）
        - 如果后续要进行较长的外部调用（LLM/Embedding 等），某些环境会对
          idle-in-transaction 的连接进行强制断开
        - 这里仅在 Session *没有待提交变更* 时 rollback，避免影响上层显式写事务
        """
        try:
            if self.db is not None and self.db.in_transaction() and not self.db.dirty and not self.db.new and not self.db.deleted:
                self.db.rollback()
        except Exception as e:
            logging.debug(f"_release_db_before_long_io ignored: {type(e).__name__}: {e}")
