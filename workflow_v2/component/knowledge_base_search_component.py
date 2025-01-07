from typing import Any

import requests

from api.settings import SCRIPT_SCHEDULER_PORT
from workflow_v2.component.base_component import BaseComponent
from workflow_v2.workflow_logging_config import WorkflowContextLogger


class KnowledgeBaseSearchComponent(BaseComponent):
    """知识库搜索组件"""

    def __init__(self, component_id: str, title: str, node_data: dict[str, Any], logger: WorkflowContextLogger):
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

    async def execute(self) -> dict[str, Any]:
        query = self.inputs.get("Query", "")
        output_list = [{"output": "xxx1"}, {"output": "xxx2"}, {"output": "xxx3"}]
        # TODO 实现知识库搜索

        return {"outputList": output_list}

    async def execute_alone(self, input_value: dict, batch_value: dict | None = None) -> dict[str, Any]:
        query = input_value.get("Query", "")
        output_list = [{"output": "xxx1"}, {"output": "xxx2"}, {"output": "xxx3"}]

        # TODO 实现知识库搜索
        return {"outputList": output_list}
