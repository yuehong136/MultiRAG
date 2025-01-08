from typing import Any
from dataclasses import dataclass
from api.db import LLMType
from api.db.services.llm_service import LLMBundle
from workflow_v2.component.base_component import BaseComponent
from workflow_v2.component.llm_component import LLMParams
from workflow_v2.workflow_logging_config import WorkflowContextLogger


@dataclass
class Intent:
    name: str
    description: str
    example: str


class IntentClassificationComponent(BaseComponent):
    """意图分类组件"""

    def __init__(self, component_id: str, title: str, node_data: dict[str, Any],
                 logger: WorkflowContextLogger, **kwargs):
        super().__init__(component_id, title, logger)
        self.llm_params: LLMParams = self._extract_llm_params(node_data)
        self.intents: list[Intent] = self._extract_intents(node_data)

        self.db = kwargs.get('db', None)
        self.user = kwargs.get('user', None)

    def _extract_llm_params(self, node_data: dict[str, Any]) -> LLMParams:
        """从节点数据中提取LLM参数"""
        params_data = node_data['data']['inputs'].get('llmParam', [])
        return LLMParams.from_params_list(params_data)

    def _extract_intents(self, node_data: dict[str, Any]) -> list[Intent]:
        """从节点数据中提取intents"""
        intents_data = node_data['data']['inputs'].get('intents', [])
        return [Intent(name=intent['name'], description=intent['desc'], example=intent['example']) for intent
                in intents_data]

    async def execute(self) -> dict[str, Any]:
        query = self.inputs.get("query", "")
        # TODO: 大模型根据query结合意图进行分类
        self.intents[0].name = "产品咨询"
        self.intents[0].description = "产品咨询"
        self.intents[0].example = "这个东西好用吗\n这个东西有什么用\n这个东西有什么好处"

        # classificationId: 0 代表没有对应的intent
        return {"classificationId": 0, "reason": "xxx"}

    async def execute_alone(self, input_value: dict, batch_value: dict | None = None) -> dict:
        query = input_value.get("query", "")
        # TODO: 大模型根据query结合意图进行分类

        # classificationId: 0 代表没有对应的intent
        return {"classificationId": 1, "reason": "xxx"}
