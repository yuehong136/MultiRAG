import asyncio
import inspect
import logging
import queue
import re
import threading
from collections.abc import AsyncIterator, Generator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import Any

from sqlalchemy.orm import Session

from api.db.db_models import LLM
from api.db.services.common_service import CommonService
from api.db.services.tenant_llm_service import LLM4Tenant, TenantLLMService, start_langfuse_observation
from common.asyncio_utils import finish_owned_work
from common.token_utils import num_tokens_from_string

_owned_bundles: ContextVar[list["LLMBundle"] | None] = ContextVar("owned_llm_bundles", default=None)


@dataclass(frozen=True, slots=True)
class _BundleTrace:
    tenant_id: str
    trace_context: tuple[tuple[str, str], ...]
    session_id: str | None
    user_id: str | None


_construction_trace: ContextVar[_BundleTrace | None] = ContextVar("llm_construction_trace", default=None)


@contextmanager
def bundle_trace_scope(tenant_id: str, trace_context: dict[str, str], session_id: str | None, user_id: str | None) -> Iterator[None]:
    """Correlate separately constructed query models without binding their tools.

    This immutable construction context carries no DB session or attached OTel
    span. Callers keep it inside one coroutine, away from streaming yields.
    """
    token = _construction_trace.set(_BundleTrace(tenant_id, tuple(trace_context.items()), session_id, user_id))
    try:
        yield
    finally:
        _construction_trace.reset(token)


async def _close_bundles(bundles: list["LLMBundle"]) -> None:
    errors: list[Exception] = []
    for bundle in reversed(bundles):
        try:
            await bundle.aclose()
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise ExceptionGroup("Failed to close task model bundles", errors)


@asynccontextmanager
async def llm_bundle_scope() -> AsyncIterator[None]:
    """Own newly constructed bundles in this coroutine and its child tasks."""
    bundles: list[LLMBundle] = []
    token = _owned_bundles.set(bundles)
    try:
        yield
    except BaseException:
        try:
            await finish_owned_work(_close_bundles(bundles))
        except asyncio.CancelledError:
            # Owned cleanup has drained. Preserve the exception from the body;
            # cancellation during cleanup must not replace a business failure.
            pass
        except Exception:
            logging.exception("Model cleanup failed while handling a task failure")
        raise
    else:
        await finish_owned_work(_close_bundles(bundles))
    finally:
        _owned_bundles.reset(token)


class LLMService(CommonService):
    model = LLM

    def __init__(self):
        super().__init__(LLM)


def get_init_tenant_llm(db: Session, user_id: str) -> list[dict[str, Any]]:
    """Seed only explicitly configured defaults, preserving zero token limits."""
    from api.db.services.model_provider_service import catalog
    from common import settings

    records: dict[tuple[str, str], dict[str, Any]] = {}
    for capability, config in (
        ("chat", settings.CHAT_CFG),
        ("embedding", settings.EMBEDDING_CFG),
        ("speech2text", settings.ASR_CFG),
        ("image2text", settings.IMAGE2TEXT_CFG),
        ("rerank", settings.RERANK_CFG),
    ):
        reference = config.get("model", "")
        if not reference:
            continue
        name, factory = TenantLLMService.split_model_name_and_factory(reference)
        factory = factory or config.get("factory", "")
        if not factory:
            raise ValueError("Configured default model requires a provider")
        declarations = [item for item in catalog(factory) if item.model_name == name]
        if declarations and capability not in declarations[0].model_types:
            raise ValueError("Configured default model does not support its capability")
        token_limit = config.get("max_tokens")
        if token_limit is None:
            token_limit = declarations[0].max_tokens if declarations else 8192
        record = {
            "tenant_id": user_id,
            "llm_factory": factory,
            "llm_name": name,
            "mdl_type": capability,
            "api_key": config.get("api_key", ""),
            "api_base": config.get("base_url", ""),
            "max_tokens": token_limit,
        }
        previous = records.get((factory, name))
        if previous and (previous["api_key"], previous["api_base"]) != (record["api_key"], record["api_base"]):
            raise ValueError("Configured defaults require distinct provider instances")
        records.setdefault((factory, name), record)
    return list(records.values())


def initialize_tenant_models(db: Session, tenant_id: str) -> None:
    """Create configured defaults and env OCR in the caller's transaction."""
    from sqlalchemy import select

    from api.db.db_models import TenantLLM, TenantModel
    from api.db.model_backfill import backfill
    from api.db.services.model_provider_service import catalog

    created: list[tuple[TenantLLM, list[str], bool]] = []
    for record in get_init_tenant_llm(db, tenant_id):
        existing = db.scalar(select(TenantLLM.id).where(TenantLLM.tenant_id == tenant_id, TenantLLM.llm_factory == record["llm_factory"], TenantLLM.llm_name == record["llm_name"]))
        if existing is not None:
            continue
        declarations = [item for item in catalog(record["llm_factory"]) if item.model_name == record["llm_name"]]
        capabilities = declarations[0].model_types if declarations else [record["mdl_type"]]
        row = TenantLLM(**record)
        db.add(row)
        created.append((row, capabilities, declarations[0].is_tools if declarations else False))
    db.flush()
    backfill(db.connection(), tenant_id)
    for row, capabilities, tools in created:
        model = db.get(TenantModel, row.id)
        if model is None:
            raise RuntimeError("Configured default was not linked")
        model.model_types = list(capabilities)
        model.is_tools = tools
    db.flush()
    TenantLLMService.ensure_ocr_models_from_env(db, tenant_id)


class _TraceObservation:
    """Keep tracing fail-open without swallowing provider/business failures."""

    def __init__(self, observation: Any = None) -> None:
        self.observation = observation
        self.ended = False

    def update(self, **kwargs: Any) -> None:
        if self.observation is not None and not self.ended:
            try:
                self.observation.update(**kwargs)
            except Exception:
                logging.warning("Langfuse model observation update failed")

    def end(self) -> None:
        if self.observation is not None and not self.ended:
            self.ended = True
            try:
                self.observation.end()
            except Exception:
                logging.warning("Langfuse model observation end failed")


class LLMBundle(LLM4Tenant):
    def __init__(self, db: Session | None, tenant_id: str, model_config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> None:
        bundles = _owned_bundles.get()
        if bundles is not None:
            bundles.append(self)
        trace = _construction_trace.get()
        inherit_trace = trace is not None and trace.tenant_id == tenant_id
        if trace is not None and inherit_trace:
            kwargs.setdefault("trace_context", dict(trace.trace_context))
            kwargs.setdefault("langfuse_session_id", trace.session_id)
            kwargs.setdefault("langfuse_user_id", trace.user_id)
        super().__init__(db, tenant_id, model_config, lang, **kwargs)
        if inherit_trace:
            # Query helpers construct inside their own short sync DB context.
            self.db = None

    def _start_langfuse_observation(self, **kwargs: Any) -> _TraceObservation:
        try:
            observation = start_langfuse_observation(
                self.langfuse,
                tenant_id=getattr(self, "langfuse_trace_tenant_id", self.tenant_id),
                session_id=getattr(self, "langfuse_session_id", None),
                user_id=getattr(self, "langfuse_user_id", None),
                **kwargs,
            )
            return _TraceObservation(observation)
        except Exception:
            logging.warning("Langfuse model observation start failed")
            return _TraceObservation()

    def bind_tools(self, toolcall_session: Any = None, tools: list[Any] | None = None) -> None:
        if not self.is_tools:
            logging.warning(f"Model {self.llm_name} does not support tool call, but you have assigned one or more tools to it!")
            return
        self.mdl.bind_tools(toolcall_session, tools)

    def _record_usage(self, used_tokens: int) -> bool:
        if not isinstance(used_tokens, int) or used_tokens <= 0:
            return True
        if self.model_config.get("llm_factory") == "Builtin":
            return True
        model_id = self.model_config.get("id")
        if model_id is None:
            raise ValueError(f"Model config for {self.model_config.get('llm_name')} is missing tenant model id")
        return bool(TenantLLMService.increase_usage_by_id(model_id, used_tokens))

    async def _record_usage_async(self, used_tokens: int) -> bool:
        """_record_usage 的异步版:async 调用链(SSE 流式等)内记账不阻塞事件循环。"""
        if not isinstance(used_tokens, int) or used_tokens <= 0:
            return True
        if self.model_config.get("llm_factory") == "Builtin":
            return True
        model_id = self.model_config.get("id")
        if model_id is None:
            raise ValueError(f"Model config for {self.model_config.get('llm_name')} is missing tenant model id")
        return bool(await TenantLLMService.increase_usage_by_id_async(model_id, used_tokens))

    def encode(self, texts: list[str | None]) -> tuple[Any, int]:
        with self._using_provider():
            return self._encode(texts)

    def _encode(self, texts: list[str | None]) -> tuple[Any, int]:
        if self.langfuse:
            generation = self._start_langfuse_observation(as_type="generation", trace_context=self.trace_context, name="encode", model=self.llm_name, input={"texts": texts})

        try:
            safe_texts: list[str] = []
            for index, text in enumerate(texts):
                # Keep one embedding per input even when parsing produced a blank chunk.
                if text is None or not text.strip():
                    marker = "None" if text is None else "whitespace-only"
                    logging.warning(
                        "LLMBundle.encode: empty input at index %d (%s) coerced to placeholder 'None' for model %s",
                        index,
                        marker,
                        self.llm_name,
                    )
                    safe_texts.append("None")
                    continue
                token_size = num_tokens_from_string(text)
                if token_size > self.max_length * 0.95:
                    target_len = int(self.max_length * 0.95)
                    safe_texts.append(text[:target_len])
                else:
                    safe_texts.append(text)

            # 避免在外部模型调用期间持有 idle-in-transaction
            self._release_db_before_long_io()
            embeddings, used_tokens = self.mdl.encode(safe_texts)

            if not self._record_usage(used_tokens):
                logging.error(f"LLMBundle.encode can't update token usage for <tenant redacted>/EMBEDDING used_tokens: {used_tokens}")

            if self.langfuse:
                generation.update(usage_details={"total_tokens": used_tokens})

            return embeddings, used_tokens
        finally:
            if self.langfuse:
                generation.end()

    def encode_queries(self, query: str | None) -> tuple[Any, int]:
        if self.langfuse:
            generation = self._start_langfuse_observation(as_type="generation", trace_context=self.trace_context, name="encode_queries", model=self.llm_name, input={"query": query})

        try:
            if query is None or not query.strip():
                marker = "None" if query is None else "whitespace-only"
                logging.warning(
                    "LLMBundle.encode_queries: empty query (%s) coerced to placeholder 'None' for model %s",
                    marker,
                    self.llm_name,
                )
                query = "None"

            # 避免在外部模型调用期间持有 idle-in-transaction
            self._release_db_before_long_io()
            emd, used_tokens = self.mdl.encode_queries(query)
            if not self._record_usage(used_tokens):
                logging.error(f"LLMBundle.encode_queries can't update token usage for <tenant redacted>/EMBEDDING used_tokens: {used_tokens}")

            if self.langfuse:
                generation.update(usage_details={"total_tokens": used_tokens})

            return emd, used_tokens
        finally:
            if self.langfuse:
                generation.end()

    def similarity(self, query: str, texts: list[str]) -> tuple[Any, int]:
        if self.langfuse:
            generation = self._start_langfuse_observation(as_type="generation", trace_context=self.trace_context, name="similarity", model=self.llm_name, input={"query": query, "texts": texts})

        try:
            # 避免在外部模型调用期间持有 idle-in-transaction
            self._release_db_before_long_io()
            sim, used_tokens = self.mdl.similarity(query, texts)
            if not self._record_usage(used_tokens):
                logging.error(f"Can't update token usage for {self.tenant_id}/RERANK used_tokens: {used_tokens}")

            if self.langfuse:
                generation.update(usage_details={"total_tokens": used_tokens})

            return sim, used_tokens
        finally:
            if self.langfuse:
                generation.end()

    def describe(self, image: Any, max_tokens: int = 300) -> str:
        if self.langfuse:
            generation = self._start_langfuse_observation(as_type="generation", trace_context=self.trace_context, name="describe", metadata={"model": self.llm_name})

        try:
            # 避免在外部模型调用期间持有 idle-in-transaction
            self._release_db_before_long_io()
            txt, used_tokens = self.mdl.describe(image)
            if not self._record_usage(used_tokens):
                logging.error(f"Can't update token usage for {self.tenant_id}/IMAGE2TEXT used_tokens: {used_tokens}")

            if self.langfuse:
                generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})

            return txt
        finally:
            if self.langfuse:
                generation.end()

    def describe_with_prompt(self, image: Any, prompt: str) -> str:
        if self.langfuse:
            generation = self._start_langfuse_observation(as_type="generation", trace_context=self.trace_context, name="describe_with_prompt", metadata={"model": self.llm_name, "prompt": prompt})

        try:
            # 避免在外部模型调用期间持有 idle-in-transaction
            self._release_db_before_long_io()
            txt, used_tokens = self.mdl.describe_with_prompt(image, prompt)
            if not self._record_usage(used_tokens):
                logging.error(f"LLMBundle.describe can't update token usage for {self.tenant_id}/IMAGE2TEXT used_tokens: {used_tokens}")

            if self.langfuse:
                generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})

            return txt
        finally:
            if self.langfuse:
                generation.end()

    def transcription(self, audio: Any) -> str:
        if self.langfuse:
            generation = self._start_langfuse_observation(as_type="generation", trace_context=self.trace_context, name="transcription", metadata={"model": self.llm_name})

        try:
            # 避免在外部模型调用期间持有 idle-in-transaction
            self._release_db_before_long_io()
            txt, used_tokens = self.mdl.transcription(audio)
            if not self._record_usage(used_tokens):
                logging.error(f"Can't update token usage for {self.tenant_id}/SEQUENCE2TXT used_tokens: {used_tokens}")

            if self.langfuse:
                generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})

            return txt
        finally:
            if self.langfuse:
                generation.end()

    def stream_transcription(self, audio: Any) -> Generator[dict[str, Any], None, None]:
        """
        流式语音转文字。
        如果底层模型支持 stream_transcription，则使用流式模式；否则回退到非流式模式。
        """
        mdl = self.mdl
        supports_stream = hasattr(mdl, "stream_transcription") and callable(mdl.stream_transcription)

        if supports_stream:
            if self.langfuse:
                generation = self._start_langfuse_observation(
                    as_type="generation",
                    trace_context=self.trace_context,
                    name="stream_transcription",
                    metadata={"model": self.llm_name},
                )
            final_text = ""
            used_tokens = 0

            try:
                # 避免在外部模型调用期间持有 idle-in-transaction
                self._release_db_before_long_io()
                for evt in mdl.stream_transcription(audio):
                    if evt.get("event") == "final":
                        final_text = evt.get("text", "")
                    yield evt

            except Exception as e:
                err = {"event": "error", "text": str(e)}
                yield err
                final_text = final_text or ""
            finally:
                if final_text:
                    used_tokens = num_tokens_from_string(final_text)
                    self._record_usage(used_tokens)

                if self.langfuse:
                    generation.update(
                        output={"output": final_text},
                        usage_details={"total_tokens": used_tokens},
                    )
                    generation.end()

            return

        # 回退到非流式模式
        if self.langfuse:
            generation = self._start_langfuse_observation(
                as_type="generation",
                trace_context=self.trace_context,
                name="stream_transcription",
                metadata={"model": self.llm_name},
            )

        try:
            # 避免在外部模型调用期间持有 idle-in-transaction
            self._release_db_before_long_io()
            full_text, used_tokens = mdl.transcription(audio)
            if not self._record_usage(used_tokens):
                logging.error(f"LLMBundle.stream_transcription can't update token usage for {self.tenant_id}/SEQUENCE2TXT used_tokens: {used_tokens}")

            if self.langfuse:
                generation.update(
                    output={"output": full_text},
                    usage_details={"total_tokens": used_tokens},
                )

            yield {
                "event": "final",
                "text": full_text,
                "streaming": False,
            }
        finally:
            if self.langfuse:
                generation.end()

    def tts(self, text: str) -> Generator[bytes, None, None]:
        if self.langfuse:
            generation = self._start_langfuse_observation(as_type="generation", trace_context=self.trace_context, name="tts", input={"text": text})

        try:
            # 避免在外部模型调用期间持有 idle-in-transaction
            self._release_db_before_long_io()
            for chunk in self.mdl.tts(text):
                if isinstance(chunk, int):
                    if not self._record_usage(chunk):
                        logging.error(f"LLMBundle.tts can't update token usage for {self.tenant_id}/TTS")
                    return
                yield chunk

        finally:
            if self.langfuse:
                generation.end()

    def _remove_reasoning_content(self, txt: str) -> str:
        if txt is None:
            return None
        first_think_start = txt.find("<think>")
        if first_think_start == -1:
            return txt

        last_think_end = txt.rfind("</think>")
        if last_think_end == -1:
            return txt

        if last_think_end < first_think_start:
            return txt

        return txt[last_think_end + len("</think>") :]

    @staticmethod
    def _clean_param(chat_partial, **kwargs):
        func = chat_partial.func
        sig = inspect.signature(func)
        support_var_args = False
        allowed_params = set()

        for param in sig.parameters.values():
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                support_var_args = True
            elif param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
                allowed_params.add(param.name)
        if support_var_args:
            return kwargs
        else:
            return {k: v for k, v in kwargs.items() if k in allowed_params}

    def _run_coroutine_sync(self, coro):
        """在同步上下文中运行协程"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        result_queue: queue.Queue = queue.Queue()

        def runner():
            try:
                result_queue.put((True, asyncio.run(coro)))
            except Exception as e:
                result_queue.put((False, e))

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join()

        success, value = result_queue.get_nowait()
        if success:
            return value
        raise value

    def _sync_from_async_stream(self, async_gen_fn, *args, **kwargs):
        """将异步生成器桥接为同步生成器"""
        result_queue: queue.Queue = queue.Queue()

        def runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def consume():
                try:
                    async for item in async_gen_fn(*args, **kwargs):
                        result_queue.put(item)
                except Exception as e:
                    result_queue.put(e)
                finally:
                    result_queue.put(StopIteration)

            loop.run_until_complete(consume())
            loop.close()

        threading.Thread(target=runner, daemon=True).start()

        while True:
            item = result_queue.get()
            if item is StopIteration:
                break
            if isinstance(item, Exception):
                raise item
            yield item

    def chat(self, system: str, history: list, gen_conf: dict[str, Any] | None = None, **kwargs) -> str:
        """
        同步 chat 方法，内部调用异步版本
        2026.01.15 已弃用
        """
        if gen_conf is None:
            gen_conf = {}
        return self._run_coroutine_sync(self.async_chat(system, history, gen_conf, **kwargs))

    def chat_streamly(self, system: str, history: list, gen_conf: dict[str, Any] | None = None, **kwargs):
        """
        同步 chat_streamly 方法，内部调用异步版本
        2026.01.15 已弃用
        """
        if gen_conf is None:
            gen_conf = {}
        ans = ""
        for txt in self._sync_from_async_stream(self.async_chat_streamly, system, history, gen_conf, **kwargs):
            if isinstance(txt, int):
                break

            if txt.endswith("</think>"):
                ans = txt[: -len("</think>")]
                continue

            if not self.verbose_tool_use:
                txt = re.sub(r"<tool_call>.*?</tool_call>", "", txt, flags=re.DOTALL)

            # concatenation has been done in async_chat_streamly
            ans = txt
            yield ans

    def _bridge_sync_stream(self, gen):
        """Bridge a synchronous generator to an async queue for async iteration."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def worker():
            try:
                for item in gen:
                    loop.call_soon_threadsafe(queue.put_nowait, item)
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, StopAsyncIteration)

        threading.Thread(target=worker, daemon=True).start()
        return queue

    async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any] | None = None, **kwargs: Any) -> str:
        with self._using_provider():
            return await self._async_chat(system, history, gen_conf, **kwargs)

    async def _async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any] | None = None, **kwargs: Any) -> str:
        """异步 chat 方法"""
        if gen_conf is None:
            gen_conf = {}

        # 优先使用原生异步方法
        if self.is_tools and getattr(self.mdl, "is_tools", False) and hasattr(self.mdl, "async_chat_with_tools"):
            base_fn = self.mdl.async_chat_with_tools
        elif hasattr(self.mdl, "async_chat"):
            base_fn = self.mdl.async_chat
        else:
            raise RuntimeError(f"Model {self.mdl} does not implement async_chat or async_chat_with_tools")

        generation = None
        if self.langfuse:
            generation = self._start_langfuse_observation(as_type="generation", trace_context=self.trace_context, name="chat", model=self.llm_name, input={"system": system, "history": history})

        try:
            chat_partial = partial(base_fn, system, history, gen_conf)
            use_kwargs = self._clean_param(chat_partial, **kwargs)

            try:
                txt, used_tokens = await chat_partial(**use_kwargs)
            except Exception as e:
                if generation:
                    generation.update(output={"error": type(e).__name__})
                raise

            txt = self._remove_reasoning_content(txt)
            if not self.verbose_tool_use:
                txt = re.sub(r"<tool_call>.*?</tool_call>", "", txt, flags=re.DOTALL)

            if self.is_tools and getattr(self.mdl, "is_tools", False) and not txt.strip():
                txt = "**ERROR**: MODEL_ERROR - Model returned an empty final answer without visible content."

            if used_tokens and not await self._record_usage_async(used_tokens):
                logging.error(f"LLMBundle.async_chat can't update token usage for {self.tenant_id}/CHAT llm_name: {self.llm_name}, used_tokens: {used_tokens}")

            if generation:
                generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})

            return txt
        finally:
            if self.langfuse:
                generation.end()

    async def async_chat_streamly(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any] | None = None, **kwargs: Any) -> AsyncIterator[str]:
        """异步流式 chat 方法"""
        if gen_conf is None:
            gen_conf = {}

        total_tokens = 0
        ans = ""

        # 优先使用原生异步流式方法
        if self.is_tools and getattr(self.mdl, "is_tools", False) and hasattr(self.mdl, "async_chat_streamly_with_tools"):
            stream_fn = getattr(self.mdl, "async_chat_streamly_with_tools", None)
        elif hasattr(self.mdl, "async_chat_streamly"):
            stream_fn = getattr(self.mdl, "async_chat_streamly", None)
        else:
            raise RuntimeError(f"Model {self.mdl} does not implement async_chat_streamly or async_chat_streamly_with_tools")

        generation = None
        if self.langfuse:
            generation = self._start_langfuse_observation(
                as_type="generation", trace_context=self.trace_context, name="chat_streamly", model=self.llm_name, input={"system": system, "history": history}
            )

        try:
            if stream_fn:
                chat_partial = partial(stream_fn, system, history, gen_conf)
                use_kwargs = self._clean_param(chat_partial, **kwargs)
                try:
                    async for txt in chat_partial(**use_kwargs):
                        if isinstance(txt, int):
                            total_tokens = txt
                            break

                        if txt.endswith("</think>") and ans.endswith("</think>"):
                            ans = ans[: -len("</think>")]

                        if not self.verbose_tool_use:
                            txt = re.sub(r"<tool_call>.*?</tool_call>", "", txt, flags=re.DOTALL)

                        ans += txt
                        yield ans
                except Exception as e:
                    if generation:
                        generation.update(output={"error": type(e).__name__})
                    raise

                if total_tokens and not await self._record_usage_async(total_tokens):
                    logging.error(f"LLMBundle.async_chat_streamly can't update token usage for {self.tenant_id}/CHAT llm_name: {self.llm_name}, used_tokens: {total_tokens}")

                if generation:
                    generation.update(output={"output": ans}, usage_details={"total_tokens": total_tokens})
                return
        finally:
            if self.langfuse:
                generation.end()

    async def async_chat_streamly_delta(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any] | None = None, **kwargs: Any) -> AsyncIterator[str]:
        """异步流式 chat 方法（增量输出）- 每次 yield 增量 txt，而非累积文本"""
        if gen_conf is None:
            gen_conf = {}

        total_tokens = 0
        ans = ""

        if self.is_tools and getattr(self.mdl, "is_tools", False) and hasattr(self.mdl, "async_chat_streamly_with_tools"):
            stream_fn = getattr(self.mdl, "async_chat_streamly_with_tools", None)
        elif hasattr(self.mdl, "async_chat_streamly"):
            stream_fn = getattr(self.mdl, "async_chat_streamly", None)
        else:
            raise RuntimeError(f"Model {self.mdl} does not implement async_chat_streamly or async_chat_streamly_with_tools")

        generation = None
        if self.langfuse:
            generation = self._start_langfuse_observation(
                as_type="generation", trace_context=self.trace_context, name="chat_streamly", model=self.llm_name, input={"system": system, "history": history}
            )

        try:
            if stream_fn:
                chat_partial = partial(stream_fn, system, history, gen_conf)
                use_kwargs = self._clean_param(chat_partial, **kwargs)
                try:
                    async for txt in chat_partial(**use_kwargs):
                        if isinstance(txt, int):
                            total_tokens = txt
                            break

                        if txt.endswith("</think>") and ans.endswith("</think>"):
                            ans = ans[: -len("</think>")]

                        if not self.verbose_tool_use:
                            txt = re.sub(r"<tool_call>.*?</tool_call>", "", txt, flags=re.DOTALL)

                        ans += txt
                        yield txt
                except Exception as e:
                    if generation:
                        generation.update(output={"error": type(e).__name__})
                    raise

                if total_tokens and not await self._record_usage_async(total_tokens):
                    logging.error(f"LLMBundle.async_chat_streamly_delta can't update token usage for {self.tenant_id}/CHAT llm_name: {self.llm_name}, used_tokens: {total_tokens}")

                if generation:
                    generation.update(output={"output": ans}, usage_details={"total_tokens": total_tokens})
                return
        finally:
            if self.langfuse:
                generation.end()
