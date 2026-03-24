from typing import Any
from dataclasses import dataclass

from api.db.services.llm_service import LLMBundle
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from common.constants import LLMType
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
        self.prompt: str = self._get_prompt()

    def _extract_llm_params(self, node_data: dict[str, Any]) -> LLMParams:
        """从节点数据中提取LLM参数"""
        params_data = node_data['data']['inputs'].get('llmParam', [])
        return LLMParams.from_params_list(params_data)

    def _extract_intents(self, node_data: dict[str, Any]) -> list[Intent]:
        """从节点数据中提取intents"""
        intents_data = node_data['data']['inputs'].get('intents', [])
        return [Intent(name=intent['name'], description=intent['desc'], example=intent['example']) for intent
                in intents_data]

    def _get_prompt(self) -> str:
        """生成用于意图分类的提示词"""
        cate_lines = []
        descriptions = []

        # 遍历 Intent 列表，生成示例和描述
        for intent in self.intents:
            # 添加示例
            for example in intent.example.split("\n"):
                if example.strip():  # 跳过空行
                    cate_lines.append(f"Question: {example.strip()}\tCategory: {intent.name}")

            # 添加描述
            if intent.description.strip():
                descriptions.append(
                    f"--------------------\nCategory: {intent.name}\nDescription: {intent.description.strip()}\n"
                )

        # 构建提示词
        return f"""
Role: You're a text classifier.  
Task: You need to categorize the user’s questions into {len(self.intents) + 1} categories, namely: {', '.join(intent.name for intent in self.intents)}, 其他意图.

Here's the description of each category:
{'\n'.join(descriptions)}

Category: 其他意图
Description: 用户意图与上述意图都不符合

You could learn from the following examples:
{'\n\n- '.join(cate_lines)}- Question: 你好，今天星期几  Category: 其他意图

You could learn from the above examples.

Requirements:
- Just mention the category names, no need for any additional words.
        """

    def _match_intent(self, result: str) -> dict | None:
        """匹配分类结果到意图"""
        for idx, intent in enumerate(self.intents, start=1):
            if intent.name.lower() in result.lower():
                return {"id": idx, "name": intent.name}
        return None

    async def _classify(self, query: str) -> dict[str, Any]:
        if not query.strip():
            return {"classificationId": 0, "reason": "Empty query"}

        input_text = "Question: " + query + "\tCategory: "
        model_config = get_model_config_by_type_and_name(
            self.db, self.user.id, LLMType.CHAT.value, self.llm_params.model_name
        )
        llm = LLMBundle(self.db, self.user.id, model_config)
        result = await llm.async_chat(
            self.prompt,
            [{"role": "user", "content": input_text}],
            {},
        )

        classification = self._match_intent(result)
        if classification is None:
            return {"classificationId": 0, "reason": "No matching intent"}
        if classification["name"] == "其他意图":
            return {"classificationId": 0, "reason": "其他意图"}

        return {"classificationId": classification["id"], "reason": classification["name"]}

    async def execute(self) -> dict[str, Any]:
        query = self.inputs.get("query", "")
        return await self._classify(query)

    async def execute_alone(self, input_value: dict, batch_value: dict | None = None) -> dict:
        query = input_value.get("query", "")
        return await self._classify(query)
