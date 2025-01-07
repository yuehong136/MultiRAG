from typing import Any

import requests

from api import settings
from api.db import LLMType
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.settings import SCRIPT_SCHEDULER_PORT
from workflow_v2.component.base_component import BaseComponent
from workflow_v2.workflow_logging_config import WorkflowContextLogger


class KnowledgeBaseSearchComponent(BaseComponent):
    """知识库搜索组件"""

    def __init__(self, component_id: str, title: str, node_data: dict[str, Any], logger: WorkflowContextLogger,
                 **kwargs):
        super().__init__(component_id, title, logger)
        self.kb_ids = node_data['data']['inputs'].get('datasetParam', '').get("kb_ids", [])
        self.similarity_threshold = node_data['data']['inputs'].get('datasetParam', '').get("similarity_threshold", 0.2)
        self.keywords_similarity_weight = node_data['data']['inputs'].get('datasetParam', '').get(
            "keywords_similarity_weight", 0.5)
        self.top_n = node_data['data']['inputs'].get('datasetParam', '').get("top_n", 8)
        self.top_k = node_data['data']['inputs'].get('datasetParam', '').get("top_k", 1024)
        self.enable_rerank = node_data['data']['inputs'].get('datasetParam', '').get("enable_rerank", False)
        self.rerank_id = node_data['data']['inputs'].get('datasetParam', '').get("rerank_id", "xxx_rerank_model")
        self.empty_response = node_data['data']['inputs'].get('datasetParam', '').get("empty_response",
                                                                                      "未找到相似结果")
        self.timeout = 30

        self.db = kwargs.get('db', None)
        self.user = kwargs.get('user', None)

    async def execute(self) -> dict[str, Any]:
        query = self.inputs.get("Query", "")
        output_list = []
        kbs = KnowledgebaseService.get_by_ids(self.db, self.kb_ids)
        if not kbs:
            output_list.append({"output": "未找到相关知识库"})
            return {"outputList": output_list}

        embd_nms = list(set([kb.embd_id for kb in kbs]))
        assert len(embd_nms) == 1, "Knowledge bases use different embedding models."

        embd_mdl = LLMBundle(self.db, kbs[0].tenant_id, LLMType.EMBEDDING, embd_nms[0])

        rerank_mdl = None
        if self.rerank_id:
            rerank_mdl = LLMBundle(self.db, kbs[0].tenant_id, LLMType.RERANK, self.rerank_id)
        kb_names = list([kb.name for kb in kbs])
        kbinfos = settings.retrievaler.retrieval(query, embd_mdl, kbs[0].tenant_id, kb_names, 1,
                                                 self.top_n,
                                                 self.similarity_threshold,
                                                 1 - self.keywords_similarity_weight,
                                                 top=1024,
                                                 aggs=False,
                                                 rerank_mdl=rerank_mdl)

        if not kbinfos["chunks"]:
            if self.empty_response and self.empty_response.strip():
                output_list.append({"output": self.empty_response})
            return {"outputList": output_list}

        output_list = [{"output": chunk["text"]} for chunk in kbinfos["chunks"]]

        return {"outputList": output_list}

    async def execute_alone(self, input_value: dict, batch_value: dict | None = None) -> dict[str, Any]:
        query = self.inputs.get("Query", "")
        output_list = []
        # TODO 实现知识库搜索
        kbs = KnowledgebaseService.get_by_ids(self.db, self.kb_ids)
        if not kbs:
            output_list.append({"output": "未找到相关知识库"})
            return {"outputList": output_list}

        embd_nms = list(set([kb.embd_id for kb in kbs]))
        assert len(embd_nms) == 1, "Knowledge bases use different embedding models."

        embd_mdl = LLMBundle(self.db, kbs[0].tenant_id, LLMType.EMBEDDING, embd_nms[0])

        rerank_mdl = None
        if self.rerank_id:
            rerank_mdl = LLMBundle(self.db, kbs[0].tenant_id, LLMType.RERANK, self.rerank_id)
        kb_names = list([kb.name for kb in kbs])
        kbinfos = settings.retrievaler.retrieval(query, embd_mdl, kbs[0].tenant_id, kb_names, 1,
                                                 self.top_n,
                                                 self.similarity_threshold,
                                                 1 - self.keywords_similarity_weight,
                                                 top=1024,
                                                 aggs=False,
                                                 rerank_mdl=rerank_mdl)

        if not kbinfos["chunks"]:
            if self.empty_response and self.empty_response.strip():
                output_list.append({"output": self.empty_response})
            return {"outputList": output_list}

        output_list = [{"output": chunk["text"]} for chunk in kbinfos["chunks"]]

        return {"outputList": output_list}
