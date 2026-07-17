import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from timeit import default_timer as timer
from typing import Any

from api.db.services.llm_service import LLMBundle
from core.prompts.generator import kb_prompt, multi_queries_gen, sufficiency_check
from core.utils.tavily_conn import Tavily

DeepResearchCallback = Callable[[str], Awaitable[None]]


class TreeStructuredQueryDecompositionRetrieval:
    def __init__(
        self,
        chat_mdl: LLMBundle,
        prompt_config: dict[str, Any],
        kb_retrieve: partial | None = None,
        kg_retrieve: partial | None = None,
        internet_enabled: bool = False,
    ) -> None:
        self.chat_mdl = chat_mdl
        self.prompt_config = prompt_config
        self._kb_retrieve = kb_retrieve
        self._kg_retrieve = kg_retrieve
        self.internet_enabled = internet_enabled
        self._lock = asyncio.Lock()

    async def _retrieve_information(self, search_query: str) -> dict[str, Any]:
        """Retrieve information from different sources"""
        # 1. Knowledge base retrieval
        kbinfos: dict[str, Any] = {"total": 0, "chunks": [], "doc_aggs": []}
        try:
            kbinfos = await self._kb_retrieve(question=search_query) if self._kb_retrieve else {"total": 0, "chunks": [], "doc_aggs": []}
            kbinfos.setdefault("total", 0)
        except Exception as e:
            logging.error(f"Knowledge base retrieval error: {e}")

        # 2. Web retrieval (if Tavily API is configured and explicitly enabled)
        try:
            if self.internet_enabled and self.prompt_config.get("tavily_api_key"):
                tav = Tavily(self.prompt_config["tavily_api_key"])
                tav_res = tav.retrieve_chunks(search_query)
                kbinfos["chunks"].extend(tav_res["chunks"])
                kbinfos["doc_aggs"].extend(tav_res["doc_aggs"])
        except Exception as e:
            logging.error(f"Web retrieval error: {e}")

        # 3. Knowledge graph retrieval (if configured)
        try:
            if self.prompt_config.get("use_kg") and self._kg_retrieve:
                ck = await self._kg_retrieve(question=search_query)
                if ck["content_with_weight"]:
                    kbinfos["chunks"].insert(0, ck)
        except Exception as e:
            logging.error(f"Knowledge graph retrieval error: {e}")

        return kbinfos

    async def _async_update_chunk_info(self, chunk_info: dict[str, Any], kbinfos: dict[str, Any]) -> None:
        async with self._lock:
            """Update chunk information for citations"""
            if not chunk_info["chunks"]:
                # If this is the first retrieval, use the retrieval results directly
                for k in chunk_info.keys():
                    chunk_info[k] = kbinfos[k]
            else:
                # Merge newly retrieved information, avoiding duplicates
                cids = [c["chunk_id"] for c in chunk_info["chunks"]]
                for c in kbinfos["chunks"]:
                    if c["chunk_id"] not in cids:
                        chunk_info["chunks"].append(c)

                dids = [d["doc_id"] for d in chunk_info["doc_aggs"]]
                for d in kbinfos["doc_aggs"]:
                    if d["doc_id"] not in dids:
                        chunk_info["doc_aggs"].append(d)

                chunk_info["total"] = chunk_info.get("total", 0) + kbinfos.get("total", 0)

    async def research(
        self,
        chunk_info: dict[str, Any],
        question: str,
        query: str,
        depth: int = 3,
        callback: DeepResearchCallback | None = None,
    ) -> None:
        if callback:
            await callback("<START_DEEP_RESEARCH>")
        try:
            await self._research(chunk_info, question, query, depth, callback)
        finally:
            if callback:
                await callback("<END_DEEP_RESEARCH>")

    async def _research(
        self,
        chunk_info: dict[str, Any],
        question: str,
        query: str,
        depth: int = 3,
        callback: DeepResearchCallback | None = None,
    ) -> str:
        if depth == 0:
            return ""
        if callback:
            await callback(f"Searching by `{query}`...")
        st = timer()
        ret = await self._retrieve_information(query)
        if callback:
            await callback("Retrieval %d results in %.1fms" % (len(ret["chunks"]), (timer() - st) * 1000))
        await self._async_update_chunk_info(chunk_info, ret)
        ret = "\n\n".join(kb_prompt(ret, self.chat_mdl.max_length * 0.5))

        if callback:
            await callback("Checking the sufficiency for retrieved information.")
        suff = await sufficiency_check(self.chat_mdl, question, ret)
        if suff.get("is_sufficient"):
            if callback:
                await callback(f"Yes, the retrieved information is sufficient for '{question}'.")
            return ret

        succ_question_info = await multi_queries_gen(self.chat_mdl, question, query, suff.get("missing_information", []), ret)
        if callback:
            await callback("Next step is to search for the following questions:</br> - " + "</br> - ".join(step["question"] for step in succ_question_info["questions"]))
        steps = []
        for step in succ_question_info["questions"]:
            steps.append(asyncio.create_task(self._research(chunk_info, step["question"], step["query"], depth - 1, callback)))
        results = await asyncio.gather(*steps, return_exceptions=True)
        return "\n".join([str(r) for r in results])
