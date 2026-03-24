from typing import Any

from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name
from core.app.tag import label_question
from common import settings
from common.constants import LLMType
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
        self.rerank_id = node_data['data']['inputs'].get('datasetParam', '').get("rerank_id", "")
        self.empty_response = node_data['data']['inputs'].get('datasetParam', '').get("empty_response", "未找到相似结果")
        self.timeout = 30

        self.db = kwargs.get('db', None)
        self.user = kwargs.get('user', None)

    def _build_embedding_model(self, kbs: list[Any]) -> LLMBundle:
        embd_keys = list(set([kb.tenant_embd_id or kb.embd_id for kb in kbs]))
        assert len(embd_keys) == 1, "Knowledge bases use different embedding models."

        if kbs[0].tenant_embd_id:
            embd_config = get_model_config_by_id(self.db, kbs[0].tenant_embd_id)
        else:
            embd_config = get_model_config_by_type_and_name(
                self.db, kbs[0].tenant_id, LLMType.EMBEDDING.value, kbs[0].embd_id
            )
        return LLMBundle(self.db, kbs[0].tenant_id, embd_config)

    def _build_rerank_model(self, kbs: list[Any]) -> LLMBundle | None:
        if not (self.enable_rerank and self.rerank_id):
            return None

        rerank_config = get_model_config_by_type_and_name(
            self.db, kbs[0].tenant_id, LLMType.RERANK.value, self.rerank_id
        )
        return LLMBundle(self.db, kbs[0].tenant_id, rerank_config)

    async def _retrieve(self, query: str, kbs: list[Any]) -> dict[str, Any]:
        embd_mdl = self._build_embedding_model(kbs)
        rerank_mdl = self._build_rerank_model(kbs)
        tenant_ids = list(set([kb.tenant_id for kb in kbs]))
        kb_names = [kb.name for kb in kbs]

        kbinfos = await settings.retriever.retrieval(
            query,
            "",
            embd_mdl,
            tenant_ids if len(tenant_ids) > 1 else tenant_ids[0],
            kb_names,
            1,
            self.top_n,
            self.similarity_threshold,
            1 - self.keywords_similarity_weight,
            top=self.top_k,
            aggs=False,
            rerank_mdl=rerank_mdl,
            rank_feature=label_question(self.db, query, kbs),
            search_mode=None,
            kb_ids=self.kb_ids,
        )
        kbinfos["chunks"] = settings.retriever.retrieval_by_children(kbinfos["chunks"], tenant_ids)
        return kbinfos

    async def execute(self) -> dict[str, Any]:
        query = self.inputs.get("Query", "")
        output_list = []
        kbs = KnowledgebaseService.get_by_ids(self.db, self.kb_ids)
        if not kbs:
            output_list.append({"output": "未找到相关知识库"})
            return {"outputList": output_list}

        kbinfos = await self._retrieve(query, kbs)

        if not kbinfos["chunks"]:
            if self.empty_response and self.empty_response.strip():
                output_list.append({"output": self.empty_response})
            else:
                output_list.append({"output": "未找到符合结果"})
            return {"outputList": output_list}

        output_list = [{"output": chunk["text"]} for chunk in kbinfos["chunks"]]

        return {"outputList": output_list}

    async def execute_alone(self, input_value: dict, batch_value: dict | None = None) -> dict[str, Any]:
        query = input_value.get("Query", "")
        output_list = []
        kbs = KnowledgebaseService.get_by_ids(self.db, self.kb_ids)
        if not kbs:
            output_list.append({"output": "未找到相关知识库"})
            return {"outputList": output_list}

        kbinfos = await self._retrieve(query, kbs)

        if not kbinfos["chunks"]:
            if self.empty_response and self.empty_response.strip():
                output_list.append({"output": self.empty_response})
            else:
                output_list.append({"output": "未找到符合结果"})
            return {"outputList": output_list}

        output_list = [{"output": chunk["text"]} for chunk in kbinfos["chunks"]]

        return {"outputList": output_list}
