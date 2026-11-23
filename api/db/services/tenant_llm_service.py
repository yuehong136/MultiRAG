import asyncio
import hashlib
import inspect
import json
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from typing import Any
from uuid import uuid4

from langfuse import Langfuse, propagate_attributes
from opentelemetry import context as otel_context
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from api.db.db_models import LLMFactories, Tenant, TenantModel, TenantModelInstance, TenantModelProvider, async_session_factory, db_connection
from api.db.model_contract import MODEL_TYPE_BITS, parse_model_id
from api.db.services.common_service import CommonService
from api.db.services.langfuse_service import TenantLangfuseService
from common import settings
from common.asyncio_utils import finish_owned_work
from common.constants import (
    MINERU_DEFAULT_CONFIG,
    MINERU_ENV_KEYS,
    OPENDATALOADER_DEFAULT_CONFIG,
    OPENDATALOADER_ENV_KEYS,
    PADDLEOCR_DEFAULT_CONFIG,
    PADDLEOCR_ENV_KEYS,
    LLMType,
)


def start_langfuse_observation(client: Any, *, tenant_id: str, session_id: str | None = None, user_id: str | None = None, **kwargs: Any) -> Any:
    """Apply SDK v4 trace attributes only while creating this observation.

    Hash the server-authorized identity tuple so shared projects cannot group
    equal session IDs across tenants/users. Never keep an OTel context attached
    across a streaming yield or forward identity to the model provider.
    """

    def identity(parts: list[str | None]) -> str:
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()

    token = otel_context.attach(otel_context.Context())
    try:
        with propagate_attributes(
            session_id=identity([tenant_id, user_id, session_id]) if session_id else None,
            user_id=identity([tenant_id, user_id]) if user_id else None,
        ):
            return client.start_observation(**kwargs)
    finally:
        otel_context.detach(token)


class LLMFactoriesService(CommonService):
    model = LLMFactories

    def __init__(self):
        super().__init__(TenantModel)


@dataclass(frozen=True, slots=True)
class PreparedTenantLLM:
    """Request-local server preparation; never accepted from API payloads or cached.

    The preparation method validates the model's tenant and capability before
    copying credentials. It carries no session, ORM row, or provider instance.
    """

    tenant_id: str
    model_config: dict[str, Any] = field(repr=False)
    langfuse_keys: tuple[str, str, str] | None = field(repr=False)


class TenantLLMService(CommonService):
    model = TenantModel

    @staticmethod
    def _decode_api_key_config(raw_api_key: str | dict[str, Any] | None) -> tuple[str, bool | None, str | None]:
        if isinstance(raw_api_key, dict):
            parsed: Any = dict(raw_api_key)
        else:
            try:
                parsed = json.loads(raw_api_key or "")
            except json.JSONDecodeError:
                return raw_api_key or "", None, None
        if not isinstance(parsed, dict):
            return raw_api_key if isinstance(raw_api_key, str) else "", None, None
        is_tools = parsed.get("is_tools")
        if "is_tools" in parsed and not isinstance(is_tools, bool):
            raise ValueError("Stored is_tools must be a boolean")
        payload = {key: value for key, value in parsed.items() if key != "is_tools"}
        raw_key = payload.get("api_key", "")
        if set(payload) <= {"api_key"} and isinstance(raw_key, str):
            return raw_key, is_tools, None
        provider_payload = json.dumps(payload, ensure_ascii=False)
        return raw_key if "api_key" in payload and isinstance(raw_key, str) else provider_payload, is_tools, provider_payload

    @classmethod
    def decode_model_config(cls, model_config: dict[str, Any]) -> dict[str, Any]:
        from api.db.provider_catalog import normalize_model_transport

        config = normalize_model_transport(model_config)
        api_key, is_tools, payload = cls._decode_api_key_config(config.get("api_key"))
        config["api_key"] = api_key
        if payload is not None:
            config["api_key_payload"] = payload
        if is_tools is not None:
            config["is_tools"] = is_tools
        return config

    @classmethod
    def resolve_model_config(cls, db: Session, model_config: dict[str, Any]) -> dict[str, Any]:
        from api.db.services.llm_service import LLMService
        from api.db.services.provider_model_resolution import resolve_model_config

        config = cls.decode_model_config(resolve_model_config(db, model_config))
        if "is_tools" not in config:
            model_name, factory = cls.split_model_name_and_factory(config["llm_name"])
            factory = factory or config.get("llm_factory")
            models = LLMService.query(db, llm_name=model_name, fid=factory) if factory else LLMService.query(db, llm_name=model_name)
            if not models and factory:
                models = LLMService.query(db, llm_name=model_name)
            config["is_tools"] = bool(models[0].is_tools) if models else False
        return config

    def __init__(self):
        super().__init__(TenantModel)

    @classmethod
    def get_api_key(cls, db: Session, tenant_id: str, model_name: str, model_type: str | LLMType | None = None) -> TenantModel | None:
        from api.db.services.provider_model_resolution import model_for_id

        try:
            model, instance, provider = model_for_id(db, model_name, tenant_id)
        except LookupError:
            return None
        if (model.status, instance.status, provider.status) != ("active", "active", "active"):
            return None
        if model_type is not None and model_type not in model.model_types:
            return None
        return model

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
    def get_model_config(cls, db: Session, tenant_id: str, llm_type: str | LLMType, llm_name: str | None = None) -> dict[str, Any]:
        from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name, get_tenant_default_model_by_type

        capability = llm_type.value if isinstance(llm_type, LLMType) else llm_type
        if llm_name:
            return get_model_config_by_type_and_name(db, tenant_id, capability, llm_name)
        return get_tenant_default_model_by_type(db, tenant_id, capability)

    @classmethod
    def resolve_model_instance_config(cls, db: Session, model_config: dict[str, Any]) -> dict[str, Any]:
        if not model_config:
            raise LookupError("Model config is required")
        from api.db.services.provider_model_resolution import resolve_model_config

        model_config = cls.decode_model_config(resolve_model_config(db, model_config))
        model_config["api_key"] = model_config.get("api_key_payload", model_config["api_key"])
        model_type = model_config.get("mdl_type") or model_config.get("model_type")
        if not model_type:
            raise ValueError("Model config is missing mdl_type/model_type")
        return model_config

    @classmethod
    def prepare_model_instance(
        cls,
        db: Session,
        tenant_id: str,
        model_config: dict[str, Any],
        *,
        expected_type: str,
        allow_unregistered_builtin: bool = False,
    ) -> PreparedTenantLLM:
        """Resolve a tenant-owned model and copy only data needed for construction."""
        raw_tenant = model_config.get("tenant_id")
        builtin = allow_unregistered_builtin and model_config.get("id") is None and model_config.get("llm_factory") == "Builtin"
        identifier = model_config.get("id")
        if not tenant_id:
            raise LookupError("Tenant is required")
        if raw_tenant is not None and raw_tenant != tenant_id:
            raise LookupError("Model does not belong to the requested tenant")
        if not builtin and (raw_tenant != tenant_id or parse_model_id(identifier) is None):
            raise LookupError("A tenant-scoped registered model is required")
        config = cls.resolve_model_instance_config(db, model_config)
        if config.get("tenant_id", tenant_id if builtin else None) != tenant_id:
            raise LookupError("Resolved model does not belong to the requested tenant")
        if (config.get("mdl_type") or config.get("model_type")) != expected_type:
            raise LookupError("Model does not support the requested capability")
        keys = TenantLangfuseService.filter_by_tenant(db, tenant_id=tenant_id)
        tracing = (keys.public_key, keys.secret_key, keys.host) if keys else None
        return PreparedTenantLLM(tenant_id, deepcopy(config), tracing)

    @classmethod
    def model_instance(cls, db: Session, tenant_id: str, model_config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> Any:
        return cls.model_instance_from_config(cls.resolve_model_instance_config(db, model_config), lang=lang, **kwargs)

    @classmethod
    def model_instance_from_config(cls, model_config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> Any:
        """Construct an already-resolved provider without opening any database."""
        model_type = model_config.get("mdl_type") or model_config.get("model_type")

        kwargs.update({"provider": model_config["llm_factory"]})
        if model_type == LLMType.EMBEDDING.value:
            from core.llm import EmbeddingModel

            if model_config["llm_factory"] == "FastEmbed":
                raise ValueError("FastEmbed is retired; configure a supported BAAI model and rebuild its vector index before switching")
            if model_config["llm_factory"] not in EmbeddingModel:
                logging.info(f"Debug: Embedding model factory not supported: {model_config['llm_factory']}")
                return None
            return EmbeddingModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"])

        elif model_type == LLMType.RERANK.value:
            from core.llm import RerankModel

            if model_config["llm_factory"] not in RerankModel:
                return None
            return RerankModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"])

        elif model_type == LLMType.IMAGE2TEXT.value:
            from core.llm import CvModel

            if model_config["llm_factory"] not in CvModel:
                logging.info(f"Debug: Image2Text model factory not supported: {model_config['llm_factory']}")
                return None
            if model_config.get("is_tools"):
                from core.llm import ChatModel

                if model_config["llm_factory"] not in ChatModel:
                    raise ValueError("Native tools are unavailable for this vision provider")
                kwargs["_tool_chat_model_factory"] = partial(ChatModel[model_config["llm_factory"]], model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"], **kwargs)
            return CvModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], lang, base_url=model_config["api_base"], **kwargs)

        elif model_type == LLMType.CHAT.value:
            from core.llm import ChatModel

            if model_config["llm_factory"] not in ChatModel:
                logging.info(f"Debug: Chat model factory not supported: {model_config['llm_factory']}")
                return None
            return ChatModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"], **kwargs)

        elif model_type == LLMType.SPEECH2TEXT.value:
            from core.llm import Seq2txtModel

            if model_config["llm_factory"] not in Seq2txtModel:
                return None
            # Preserve existing providers while forwarding the gateway URL for FuturMix.
            speech_kwargs = {"base_url": model_config["api_base"]} if model_config["llm_factory"] == "FuturMix" else {}
            return Seq2txtModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], **speech_kwargs)

        elif model_type == LLMType.TTS.value:
            from core.llm import TTSModel

            if model_config["llm_factory"] not in TTSModel:
                return None
            return TTSModel[model_config["llm_factory"]](model_config["api_key"], model_config["llm_name"], base_url=model_config["api_base"])

        elif model_type == LLMType.OCR.value:
            from core.llm import OcrModel

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
    def increase_usage_by_id(cls, tenant_model_id: str | None, used_tokens: int) -> int:
        try:
            if parse_model_id(tenant_model_id) is None or not isinstance(used_tokens, int) or used_tokens <= 0:
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
    async def increase_usage_by_id_async(cls, tenant_model_id: str | None, used_tokens: int) -> int:
        """increase_usage_by_id 的异步版:async 调用链(SSE 流式等)内记账不阻塞事件循环。

        与同步版同语义:独立短会话、best-effort 吞异常;非 PG 后端无异步引擎时
        退回线程池跑同步版。
        """
        try:
            if parse_model_id(tenant_model_id) is None or not isinstance(used_tokens, int) or used_tokens <= 0:
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
    def delete_by_tenant_id(cls, db: Session, tenant_id: str) -> int:
        providers = select(TenantModelProvider.id).where(TenantModelProvider.tenant_id == tenant_id)
        result = db.execute(delete(TenantModel).where(TenantModel.provider_id.in_(providers)))
        db.execute(delete(TenantModelInstance).where(TenantModelInstance.provider_id.in_(providers)))
        db.execute(delete(TenantModelProvider).where(TenantModelProvider.tenant_id == tenant_id))
        return result.rowcount

    @staticmethod
    def _collect_ocr_env_config(keys: list[str], defaults: dict[str, Any]) -> dict[str, Any] | None:
        config = dict(defaults)
        found = False
        for key in keys:
            value = os.environ.get(key)
            if value:
                found = True
                config[key] = value
        return config if found else None

    @classmethod
    def _ensure_ocr_from_env(cls, db: Session, tenant_id: str, provider: str, config: dict[str, Any] | None) -> str | None:
        """Register env OCR in the caller's transaction."""
        if not config:
            return None
        from api.db.services.provider_model_resolution import model_records

        if db.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()) is None:
            raise LookupError("Tenant not found")
        stored_provider = db.scalar(select(TenantModelProvider).where(TenantModelProvider.tenant_id == tenant_id, TenantModelProvider.provider_name == provider))
        if stored_provider is not None and stored_provider.status != "active":
            return None
        rows = db.execute(model_records().where(TenantModelProvider.tenant_id == tenant_id, TenantModelProvider.provider_name == provider).order_by(TenantModel.id)).all()
        for model, instance, _ in rows:
            if "ocr" not in model.model_types:
                continue
            try:
                payload = json.loads(instance.api_key or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and all(payload.get(key, value) == value for key, value in config.items()):
                return model.id if model.status == instance.status == "active" else None
        if stored_provider is None:
            stored_provider = TenantModelProvider(id=uuid4().hex, tenant_id=tenant_id, provider_name=provider, status="active")
            db.add(stored_provider)
            db.flush()
        used_names = set(db.scalars(select(TenantModelInstance.instance_name).where(TenantModelInstance.provider_id == stored_provider.id)))
        index = 1
        name = f"{provider.lower()}-from-env-{index}"
        while name in used_names:
            index += 1
            name = f"{provider.lower()}-from-env-{index}"
        instance = TenantModelInstance(id=uuid4().hex, provider_id=stored_provider.id, instance_name=name, api_key=json.dumps(config), base_url="", region="", status="active")
        db.add(instance)
        db.flush()
        model = TenantModel(provider_id=stored_provider.id, instance_id=instance.id, model_name=name, model_type=MODEL_TYPE_BITS["ocr"], status="active", extra={"max_tokens": 0})
        db.add(model)
        db.flush()
        return model.id

    @classmethod
    def ensure_mineru_from_env(cls, db: Session, tenant_id: str) -> str | None:
        return cls._ensure_ocr_from_env(db, tenant_id, "MinerU", cls._collect_ocr_env_config(MINERU_ENV_KEYS, MINERU_DEFAULT_CONFIG))

    @classmethod
    def ensure_paddleocr_from_env(cls, db: Session, tenant_id: str) -> str | None:
        return cls._ensure_ocr_from_env(db, tenant_id, "PaddleOCR", cls._collect_ocr_env_config(PADDLEOCR_ENV_KEYS, PADDLEOCR_DEFAULT_CONFIG))

    @classmethod
    def ensure_opendataloader_from_env(cls, db: Session, tenant_id: str) -> str | None:
        return cls._ensure_ocr_from_env(db, tenant_id, "OpenDataLoader", cls._collect_ocr_env_config(OPENDATALOADER_ENV_KEYS, OPENDATALOADER_DEFAULT_CONFIG))

    @classmethod
    def ensure_ocr_models_from_env(cls, db: Session, tenant_id: str) -> None:
        for ensure in (cls.ensure_mineru_from_env, cls.ensure_paddleocr_from_env, cls.ensure_opendataloader_from_env):
            ensure(db, tenant_id)


class LLM4Tenant:
    def __init__(self, db: Session | None, tenant_id: str, model_config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> None:
        kwargs = self._configure(db, tenant_id, model_config, **kwargs)
        model_config = self.model_config

        if db is None:
            with db_connection() as init_db:
                self._initialize(init_db, tenant_id, model_config, lang, **kwargs)
        else:
            self._initialize(db, tenant_id, model_config, lang, **kwargs)

    def _configure(self, db: Session | None, tenant_id: str, model_config: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        model_config = TenantLLMService.decode_model_config(model_config)
        self.db = db
        self.tenant_id = tenant_id
        self.model_config = model_config
        self.llm_type = model_config.get("mdl_type") or model_config.get("model_type")
        self.llm_name = model_config.get("llm_name")
        self.langfuse: Any = None
        self.trace_context = dict(kwargs.pop("trace_context", None) or {})
        self.langfuse_session_id = kwargs.pop("langfuse_session_id", None)
        self.langfuse_user_id = kwargs.pop("langfuse_user_id", None)
        self.langfuse_trace_tenant_id = kwargs.pop("langfuse_trace_tenant_id", tenant_id)
        self.verbose_tool_use = kwargs.get("verbose_tool_use")
        self._provider_condition = threading.Condition()
        self._provider_closed = False
        return kwargs

    def _initialize_prepared(self, prepared: PreparedTenantLLM, lang: str, **kwargs: Any) -> None:
        """Build from a server preparation without invoking the legacy DB path."""
        config = deepcopy(prepared.model_config)
        kwargs = self._configure(None, prepared.tenant_id, config, **kwargs)
        self.mdl = TenantLLMService.model_instance_from_config(config, lang=lang, **kwargs)
        assert self.mdl, f"Can't find model for {prepared.tenant_id}/{self.llm_type}/{self.llm_name}"
        self.max_length = config.get("max_tokens", 8192)
        self.is_tools = config.get("is_tools", False)
        self._initialize_langfuse(prepared.langfuse_keys)

    def _resource_condition(self) -> threading.Condition:
        # Legacy subclasses may initialize their provider without calling super.
        return self.__dict__.setdefault("_provider_condition", threading.Condition())

    @contextmanager
    def _using_provider(self) -> Iterator[None]:
        condition = self._resource_condition()
        with condition:
            if getattr(self, "_provider_closed", False) or getattr(self, "_provider_closing", False):
                raise RuntimeError("Model bundle is closed")
            self._provider_calls = getattr(self, "_provider_calls", 0) + 1
        try:
            yield
        finally:
            with condition:
                self._provider_calls -= 1
                condition.notify_all()

    def _wait_for_provider(self) -> bool:
        condition = self._resource_condition()
        with condition:
            condition.wait_for(lambda: not getattr(self, "_provider_closing", False))
            if getattr(self, "_provider_closed", False):
                return False
            self._provider_closing = True
            condition.wait_for(lambda: not getattr(self, "_provider_calls", 0))
            return True

    async def aclose(self) -> None:
        """Close this bundle's provider after outstanding synchronous encodes.

        Langfuse's SDK resource manager is shared by public key and owns its
        shutdown. A per-task bundle must not shut down another task's tracer.
        """
        await finish_owned_work(self._close_provider())

    async def _close_provider(self) -> None:
        if not await asyncio.to_thread(self._wait_for_provider):
            return
        try:
            model = getattr(self, "mdl", None)
            close = getattr(model, "aclose", None) or getattr(model, "close", None)
            targets = [model] if callable(close) else [getattr(model, "client", None), getattr(model, "async_client", None)]
            seen: set[int] = set()
            errors: list[Exception] = []
            for target in targets:
                if target is None or id(target) in seen:
                    continue
                seen.add(id(target))
                close = getattr(target, "aclose", None) or getattr(target, "close", None)
                if not callable(close):
                    continue
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    errors.append(exc)
            self.langfuse = None
            self.trace_context = {}
            if errors:
                raise ExceptionGroup("Failed to close owned model resources", errors)
        finally:
            with self._resource_condition():
                self._provider_closed = True
                self._provider_closing = False
                self._resource_condition().notify_all()

    def _initialize(
        self,
        db: Session,
        tenant_id: str,
        model_config: dict[str, Any],
        lang: str,
        **kwargs: Any,
    ) -> None:
        self.mdl = TenantLLMService.model_instance(db, tenant_id, model_config, lang=lang, **kwargs)
        assert self.mdl, f"Can't find model for {tenant_id}/{self.llm_type}/{self.llm_name}"
        self.max_length = model_config.get("max_tokens", 8192)
        self.is_tools = model_config.get("is_tools", False)

        langfuse_keys = TenantLangfuseService.filter_by_tenant(db, tenant_id=tenant_id)
        self._initialize_langfuse((langfuse_keys.public_key, langfuse_keys.secret_key, langfuse_keys.host) if langfuse_keys else None)

    def _initialize_langfuse(self, keys: tuple[str, str, str] | None) -> None:
        if keys:
            # 零 preflight：auth_check 是阻塞 HTTP（默认 5s 超时；经 run_sync 构造 LLMBundle 时
            # 会冻结整个事件循环）。凭据有效性在配置写入期校验（langfuse_api_service）；此处 fail-open。
            try:
                self.langfuse = Langfuse(public_key=keys[0], secret_key=keys[1], host=keys[2])
                if not self.trace_context:
                    self.trace_context = {"trace_id": self.langfuse.create_trace_id()}
            except Exception:
                logging.warning("Langfuse tracer init failed; tracing disabled for this bundle")
                self.langfuse = None
                self.trace_context = {}

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
