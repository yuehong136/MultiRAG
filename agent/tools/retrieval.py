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
import asyncio
import json
import os
import re
from abc import ABC
from functools import partial
from typing import Any

from agent.tools.base import ToolBase, ToolMeta, ToolParamBase
from api.db.db_models import db_connection
from api.db.joint_services import memory_message_service
from api.db.joint_services.tenant_model_service import build_bundle_by_id_async, build_default_bundle_async, build_named_bundle_async
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.memory_service import MemoryService
from common import settings
from common.connection_utils import timeout
from common.constants import LLMType
from common.metadata_utils import apply_meta_data_filter
from core.app.tag import label_question
from core.prompts.generator import cross_languages, kb_prompt, memory_prompt


class RetrievalParam(ToolParamBase):
    """
    Define the Retrieval component parameters.
    """

    def __init__(self):
        self.meta: ToolMeta = {
            "name": "search_my_dateset",
            "description": "This tool can be utilized for relevant content searching in the datasets.",
            "parameters": {
                "query": {
                    "type": "string",
                    "description": "The keywords to search the dataset. The keywords should be the most important words/terms(includes synonyms) from the original request.",
                    "default": "",
                    "required": True,
                }
            },
        }
        super().__init__()
        self.function_name = "search_my_dateset"
        self.description = "This tool can be utilized for relevant content searching in the datasets."
        self.similarity_threshold = 0.2
        self.keywords_similarity_weight = 0.5
        self.top_n = 8
        self.top_k = 1024
        self.dataset_ids = []
        self.kb_ids = []  # Deprecated: keep for backward compatibility.
        self.memory_ids = []
        self.kb_vars = []
        self.rerank_id = ""
        self.empty_response = ""
        self.use_kg = False
        self.cross_languages = []
        self.toc_enhance = False
        self.meta_data_filter = {}

    def check(self):
        self.check_decimal_float(self.similarity_threshold, "[Retrieval] Similarity threshold")
        self.check_decimal_float(self.keywords_similarity_weight, "[Retrieval] Keyword similarity weight")
        self.check_positive_number(self.top_n, "[Retrieval] Top N")

    def get_input_form(self) -> dict[str, dict]:
        return {"query": {"name": "Query", "type": "line"}}


class Retrieval(ToolBase, ABC):
    component_name = "Retrieval"

    @property
    def _dataset_ids(self):
        return getattr(self._param, "dataset_ids", None) or getattr(self._param, "kb_ids", None) or []

    async def _retrieve_memory(self, query_text: str):
        """Retrieve from memory storage.

        整体入线程池：块内是纯同步链（DB + query_message 内的 embedding HTTP 与消息库检索），
        不持有调用方 Session——线程池一次解除全部阻塞面（只桥 DB 会漏掉 embedding HTTP）。
        """
        return await asyncio.to_thread(self._retrieve_memory_sync, query_text)

    def _retrieve_memory_sync(self, query_text: str):
        with db_connection() as db:
            memory_ids: list[str] = list(self._param.memory_ids)
            user_id = getattr(self._param, "user_id", None)
            if user_id and isinstance(user_id, str) and re.match(r"^{.*}$", user_id):
                user_id = self._canvas.get_variable_value(user_id)
            memory_list = MemoryService.get_by_ids(db, memory_ids)
            if not memory_list:
                raise Exception("No memory is selected.")

            embd_names = list({memory.embd_id for memory in memory_list})
            assert len(embd_names) == 1, "Memory use different embedding models."

            vars = self.get_input_elements_from_text(query_text)
            vars = {k: o["value"] for k, o in vars.items()}
            query = self.string_format(query_text, vars)

            # Query message
            filter_dict: dict = {"memory_id": memory_ids}
            if user_id:
                filter_dict["user_id"] = user_id
            message_list = memory_message_service.query_message(
                db,
                filter_dict,
                {
                    "query": query,
                    "similarity_threshold": self._param.similarity_threshold,
                    "keywords_similarity_weight": self._param.keywords_similarity_weight,
                    "top_n": self._param.top_n,
                },
            )
            if not message_list:
                self.set_output("formalized_content", self._param.empty_response)
                return ""

            formated_content = "\n".join(memory_prompt(message_list, 200000))
            # Set formalized_content output
            self.set_output("formalized_content", formated_content)

            return formated_content

    @timeout(int(os.environ.get("COMPONENT_EXEC_TIMEOUT", 12)))
    async def _invoke_async(self, **kwargs):
        if self.check_if_canceled("Retrieval processing"):
            return

        if not kwargs.get("query"):
            self.set_output("formalized_content", self._param.empty_response)
            return

        if hasattr(self._param, "retrieval_from") and self._param.retrieval_from == "dataset":
            return await self._retrieve_kb(kwargs["query"])
        elif hasattr(self._param, "retrieval_from") and self._param.retrieval_from == "memory":
            return await self._retrieve_memory(kwargs["query"])
        elif self._dataset_ids:
            return await self._retrieve_kb(kwargs["query"])
        elif hasattr(self._param, "memory_ids") and self._param.memory_ids:
            return await self._retrieve_memory(kwargs["query"])
        else:
            self.set_output("formalized_content", self._param.empty_response)
            return

    def _resolve_kbs(self) -> tuple[list[str], list[str], list]:
        """解析 dataset_ids（含 @变量 引用）→ (kb_ids, filtered_kb_ids, kbs)。纯同步 DB。"""
        with db_connection() as db:
            kb_ids: list[str] = []
            for id in self._dataset_ids:
                if id.find("@") < 0:
                    kb_ids.append(id)
                    continue
                kb_nm = self._canvas.get_variable_value(id)
                # if kb_nm is a list
                kb_nm_list = kb_nm if isinstance(kb_nm, list) else [kb_nm]
                for nm_or_id in kb_nm_list:
                    e, kb = KnowledgebaseService.get_by_name(db, nm_or_id, self._canvas._tenant_id)
                    if not e:
                        raise Exception(f"Dataset({nm_or_id}) does not exist.")
                kb_ids.append(kb.id)

            filtered_kb_ids: list[str] = list({kb_id for kb_id in kb_ids if kb_id})
            kbs = KnowledgebaseService.get_by_ids(db, filtered_kb_ids)
            if not kbs:
                raise Exception("No dataset is selected.")
            return kb_ids, filtered_kb_ids, kbs

    @staticmethod
    def _flatted_metas(kb_ids: list[str]) -> Any:
        """元数据读取（纯同步 DB，自开短连接）。"""
        with db_connection() as db:
            return DocMetadataService.get_flatted_meta_by_kbs(db, kb_ids)

    @staticmethod
    def _rank_feature(query: str, kbs: list) -> Any:
        """tag 特征（同步：tag 缓存 + doc-store 查询；自开短连接）。"""
        with db_connection() as db:
            return label_question(db, query, kbs)

    async def _retrieve_kb(self, query_text: str):
        """Retrieve from knowledge base.

        DB 面一律按需自开短连接、用完即释放（helper 内部 to_thread + 剥离会话）：
        **不**用一个会话包住整个函数——下方的检索/LLM await 是分钟级的，会把连接以
        idle-in-transaction 状态钉死整个检索期。
        """
        kb_ids, filtered_kb_ids, kbs = await asyncio.to_thread(self._resolve_kbs)

        # Keep the embedding-equivalence check aligned with RAGFlow: tenant_embd_id
        # selects the concrete tenant model row, while embd_id defines model equivalence.
        embd_keys = list({kb.embd_id for kb in kbs})
        assert len(embd_keys) == 1, "Knowledge bases use different embedding models."

        embd_mdl = None
        if embd_keys:
            if kbs[0].tenant_embd_id:
                embd_mdl = await build_bundle_by_id_async(self._canvas.get_tenant_id(), kbs[0].tenant_embd_id)
            else:
                embd_mdl = await build_named_bundle_async(self._canvas.get_tenant_id(), LLMType.EMBEDDING.value, kbs[0].embd_id)

        rerank_mdl = None
        if self._param.rerank_id:
            rerank_mdl = await build_named_bundle_async(kbs[0].tenant_id, LLMType.RERANK.value, self._param.rerank_id)

        vars = self.get_input_elements_from_text(query_text)
        vars = {k: o["value"] for k, o in vars.items()}
        query = self.string_format(query_text, vars)

        doc_ids = []
        if self._param.meta_data_filter != {}:
            metas = await asyncio.to_thread(self._flatted_metas, kb_ids)

            def _resolve_manual_filter(flt: dict) -> dict:
                pat = re.compile(self.variable_ref_patt)
                s = flt.get("value", "")
                out_parts = []
                last = 0

                for m in pat.finditer(s):
                    out_parts.append(s[last : m.start()])
                    key = m.group(1)
                    v = self._canvas.get_variable_value(key)
                    if v is None:
                        rep = ""
                    elif isinstance(v, partial):
                        buf = []
                        for chunk in v():
                            buf.append(chunk)
                        rep = "".join(buf)
                    elif isinstance(v, str):
                        rep = v
                    else:
                        rep = json.dumps(v, ensure_ascii=False)

                    out_parts.append(rep)
                    last = m.end()

                out_parts.append(s[last:])
                flt["value"] = "".join(out_parts)
                return flt

            chat_mdl = None
            if self._param.meta_data_filter.get("method") in ["auto", "semi_auto"]:
                chat_mdl = await build_default_bundle_async(self._canvas.get_tenant_id(), LLMType.CHAT)

            doc_ids = await apply_meta_data_filter(
                self._param.meta_data_filter,
                metas,
                query,
                chat_mdl,
                doc_ids,
                _resolve_manual_filter if self._param.meta_data_filter.get("method") == "manual" else None,
            )

        if self._param.cross_languages:
            query = await cross_languages(kbs[0].tenant_id, None, query, self._param.cross_languages)

        tenant_ids = list({kb.tenant_id for kb in kbs})
        if kbs:
            kb_names = [kb.name for kb in kbs]
            query = re.sub(r"^user[:：\s]*", "", query, flags=re.IGNORECASE)
            rank_feature = await asyncio.to_thread(self._rank_feature, query, kbs)
            kbinfos = await settings.retriever.retrieval(
                query,
                "",
                embd_mdl,
                tenant_ids,
                kb_names,
                1,
                self._param.top_n,
                self._param.similarity_threshold,
                1 - self._param.keywords_similarity_weight,
                doc_ids=doc_ids,
                aggs=False,
                rerank_mdl=rerank_mdl,
                rank_feature=rank_feature,
                kb_ids=filtered_kb_ids,
            )

            if self.check_if_canceled("Retrieval processing"):
                return

            # TOC增强和知识图谱检索
            if self._param.toc_enhance:
                chat_mdl = await build_default_bundle_async(self._canvas._tenant_id, LLMType.CHAT)
                cks = await settings.retriever.retrieval_by_toc(query, kbinfos["chunks"], tenant_ids, kb_names, chat_mdl, self._param.top_n)
                if self.check_if_canceled("Retrieval processing"):
                    return
                if cks:
                    kbinfos["chunks"] = cks
            kbinfos["chunks"] = settings.retriever.retrieval_by_children(kbinfos["chunks"], [kb.tenant_id for kb in kbs])
            if self._param.use_kg:
                kg_chat_mdl = await build_default_bundle_async(self._canvas.get_tenant_id(), LLMType.CHAT)
                ck = await settings.kg_retriever.retrieval(query, tenant_ids, kb_ids, embd_mdl, kg_chat_mdl)
                if self.check_if_canceled("Retrieval processing"):
                    return
                if ck["content_with_weight"]:
                    kbinfos["chunks"].insert(0, ck)
        else:
            kbinfos = {"chunks": [], "doc_aggs": []}

        if self._param.use_kg and kbs:
            kg2_chat_mdl = await build_default_bundle_async(kbs[0].tenant_id, LLMType.CHAT)
            ck = await settings.kg_retriever.retrieval(query, tenant_ids, filtered_kb_ids, embd_mdl, kg2_chat_mdl)
            if self.check_if_canceled("Retrieval processing"):
                return
            if ck["content_with_weight"]:
                ck["content"] = ck["content_with_weight"]
                del ck["content_with_weight"]
                kbinfos["chunks"].insert(0, ck)

        for ck in kbinfos["chunks"]:
            if "vector" in ck:
                del ck["vector"]
            if "content_ltks" in ck:
                del ck["content_ltks"]

        if not kbinfos["chunks"]:
            self.set_output("formalized_content", self._param.empty_response)
            return

        # Format the chunks for JSON output (similar to how other tools do it)
        json_output = kbinfos["chunks"].copy()

        self._canvas.add_reference(kbinfos["chunks"], kbinfos["doc_aggs"])
        form_cnt = "\n".join(kb_prompt(kbinfos, 200000, True))

        # Set both formalized content and JSON output
        self.set_output("formalized_content", form_cnt)
        self.set_output("json", json_output)

        return form_cnt

    @timeout(int(os.environ.get("COMPONENT_EXEC_TIMEOUT", 12)))
    def _invoke(self, **kwargs):
        return asyncio.run(self._invoke_async(**kwargs))

    def thoughts(self) -> str:
        return """
Keywords: {}
Looking for the most relevant articles.
        """.format(self.get_input().get("query", "-_-!"))
