import asyncio
import inspect
import logging
import queue
import re
import threading
from collections.abc import Generator
from functools import partial
from typing import Any

from sqlalchemy.orm import Session

from api.db.db_models import LLM
from api.db.services.common_service import CommonService
from api.db.services.tenant_llm_service import LLM4Tenant, TenantLLMService
from common.constants import LLMType
from common.token_utils import num_tokens_from_string


class LLMService(CommonService):
    model = LLM

    def __init__(self):
        super().__init__(LLM)


def get_init_tenant_llm(db, user_id):
    from common import settings

    tenant_llm = []

    model_configs = {
        LLMType.CHAT: settings.CHAT_CFG,
        LLMType.EMBEDDING: settings.EMBEDDING_CFG,
        LLMType.SPEECH2TEXT: settings.ASR_CFG,
        LLMType.IMAGE2TEXT: settings.IMAGE2TEXT_CFG,
        LLMType.RERANK: settings.RERANK_CFG,
    }

    seen = set()
    factory_configs = []
    for factory_config in [
        settings.CHAT_CFG,
        settings.EMBEDDING_CFG,
        settings.ASR_CFG,
        settings.IMAGE2TEXT_CFG,
        settings.RERANK_CFG,
    ]:
        factory_name = factory_config["factory"]
        if factory_name not in seen:
            seen.add(factory_name)
            factory_configs.append(factory_config)

    for factory_config in factory_configs:
        for llm in LLMService.query(db, fid=factory_config["factory"]):
            tenant_llm.append(
                {
                    "tenant_id": user_id,
                    "llm_factory": factory_config["factory"],
                    "llm_name": llm.llm_name,
                    "mdl_type": llm.mdl_type,
                    "api_key": model_configs.get(llm.mdl_type, {}).get("api_key", factory_config["api_key"]),
                    "api_base": model_configs.get(llm.mdl_type, {}).get("base_url", factory_config["base_url"]),
                    "max_tokens": llm.max_tokens if llm.max_tokens else 8192,
                }
            )

    for buildin_embedding_model in settings.BUILTIN_EMBEDDING_MODELS:
        mdlnm, fid = TenantLLMService.split_model_name_and_factory(buildin_embedding_model)
        tenant_llm.append(
            {
                "tenant_id": user_id,
                "llm_factory": fid,
                "llm_name": mdlnm,
                "mdl_type": "embedding",
                "api_key": "",
                "api_base": "",
                "max_tokens": 1024 if buildin_embedding_model == "BAAI/bge-large-zh-v1.5@BAAI" else 512,
            }
        )

    unique = {}
    for item in tenant_llm:
        key = (item["tenant_id"], item["llm_factory"], item["llm_name"])
        if key not in unique:
            unique[key] = item
    return list(unique.values())


class LLMBundle(LLM4Tenant):
    def __init__(self, db: Session | None, tenant_id: str, model_config: dict, lang: str = "Chinese", **kwargs):
        super().__init__(db, tenant_id, model_config, lang, **kwargs)

    def bind_tools(self, toolcall_session, tools):
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

    def encode(self, texts: list):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="encode", model=self.llm_name, input={"texts": texts})

        safe_texts = []
        for text in texts:
            token_size = num_tokens_from_string(text)
            if token_size > self.max_length:
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
            generation.end()

        return embeddings, used_tokens

    def encode_queries(self, query: str):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="encode_queries", model=self.llm_name, input={"query": query})

        # 避免在外部模型调用期间持有 idle-in-transaction
        self._release_db_before_long_io()
        emd, used_tokens = self.mdl.encode_queries(query)
        if not self._record_usage(used_tokens):
            logging.error(f"LLMBundle.encode_queries can't update token usage for <tenant redacted>/EMBEDDING used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(usage_details={"total_tokens": used_tokens})
            generation.end()

        return emd, used_tokens

    def similarity(self, query: str, texts: list):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="similarity", model=self.llm_name, input={"query": query, "texts": texts})

        # 避免在外部模型调用期间持有 idle-in-transaction
        self._release_db_before_long_io()
        sim, used_tokens = self.mdl.similarity(query, texts)
        if not self._record_usage(used_tokens):
            logging.error(f"Can't update token usage for {self.tenant_id}/RERANK used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(usage_details={"total_tokens": used_tokens})
            generation.end()

        return sim, used_tokens

    def describe(self, image, max_tokens: int = 300):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="describe", metadata={"model": self.llm_name})

        # 避免在外部模型调用期间持有 idle-in-transaction
        self._release_db_before_long_io()
        txt, used_tokens = self.mdl.describe(image)
        if not self._record_usage(used_tokens):
            logging.error(f"Can't update token usage for {self.tenant_id}/IMAGE2TEXT used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})
            generation.end()

        return txt

    def describe_with_prompt(self, image, prompt):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="describe_with_prompt", metadata={"model": self.llm_name, "prompt": prompt})

        # 避免在外部模型调用期间持有 idle-in-transaction
        self._release_db_before_long_io()
        txt, used_tokens = self.mdl.describe_with_prompt(image, prompt)
        if not self._record_usage(used_tokens):
            logging.error(f"LLMBundle.describe can't update token usage for {self.tenant_id}/IMAGE2TEXT used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})
            generation.end()

        return txt

    def transcription(self, audio):
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="transcription", metadata={"model": self.llm_name})

        # 避免在外部模型调用期间持有 idle-in-transaction
        self._release_db_before_long_io()
        txt, used_tokens = self.mdl.transcription(audio)
        if not self._record_usage(used_tokens):
            logging.error(f"Can't update token usage for {self.tenant_id}/SEQUENCE2TXT used_tokens: {used_tokens}")

        if self.langfuse:
            generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})
            generation.end()

        return txt

    def stream_transcription(self, audio):
        """
        流式语音转文字。
        如果底层模型支持 stream_transcription，则使用流式模式；否则回退到非流式模式。
        """
        mdl = self.mdl
        supports_stream = hasattr(mdl, "stream_transcription") and callable(mdl.stream_transcription)

        if supports_stream:
            if self.langfuse:
                generation = self.langfuse.start_generation(
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
            generation = self.langfuse.start_generation(
                trace_context=self.trace_context,
                name="stream_transcription",
                metadata={"model": self.llm_name},
            )

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
            generation.end()

        yield {
            "event": "final",
            "text": full_text,
            "streaming": False,
        }

    def tts(self, text: str) -> Generator[bytes, None, None]:
        if self.langfuse:
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="tts", input={"text": text})

        # 避免在外部模型调用期间持有 idle-in-transaction
        self._release_db_before_long_io()
        for chunk in self.mdl.tts(text):
            if isinstance(chunk, int):
                if not self._record_usage(chunk):
                    logging.error(f"LLMBundle.tts can't update token usage for {self.tenant_id}/TTS")
                return
            yield chunk

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

    async def async_chat(self, system: str, history: list, gen_conf: dict[str, Any] | None = None, **kwargs) -> str:
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
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="chat", model=self.llm_name, input={"system": system, "history": history})

        chat_partial = partial(base_fn, system, history, gen_conf)
        use_kwargs = self._clean_param(chat_partial, **kwargs)

        try:
            txt, used_tokens = await chat_partial(**use_kwargs)
        except Exception as e:
            if generation:
                generation.update(output={"error": str(e)})
                generation.end()
            raise

        txt = self._remove_reasoning_content(txt)
        if not self.verbose_tool_use:
            txt = re.sub(r"<tool_call>.*?</tool_call>", "", txt, flags=re.DOTALL)

        if used_tokens and not await self._record_usage_async(used_tokens):
            logging.error(f"LLMBundle.async_chat can't update token usage for {self.tenant_id}/CHAT llm_name: {self.llm_name}, used_tokens: {used_tokens}")

        if generation:
            generation.update(output={"output": txt}, usage_details={"total_tokens": used_tokens})
            generation.end()

        return txt

    async def async_chat_streamly(self, system: str, history: list, gen_conf: dict[str, Any] | None = None, **kwargs):
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
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="chat_streamly", model=self.llm_name, input={"system": system, "history": history})

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
                    generation.update(output={"error": str(e)})
                    generation.end()
                raise

            if total_tokens and not await self._record_usage_async(total_tokens):
                logging.error(f"LLMBundle.async_chat_streamly can't update token usage for {self.tenant_id}/CHAT llm_name: {self.llm_name}, used_tokens: {total_tokens}")

            if generation:
                generation.update(output={"output": ans}, usage_details={"total_tokens": total_tokens})
                generation.end()
            return

    async def async_chat_streamly_delta(self, system: str, history: list, gen_conf: dict[str, Any] | None = None, **kwargs):
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
            generation = self.langfuse.start_generation(trace_context=self.trace_context, name="chat_streamly", model=self.llm_name, input={"system": system, "history": history})

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
                    generation.update(output={"error": str(e)})
                    generation.end()
                raise

            if total_tokens and not await self._record_usage_async(total_tokens):
                logging.error(f"LLMBundle.async_chat_streamly_delta can't update token usage for {self.tenant_id}/CHAT llm_name: {self.llm_name}, used_tokens: {total_tokens}")

            if generation:
                generation.update(output={"output": ans}, usage_details={"total_tokens": total_tokens})
                generation.end()
            return
