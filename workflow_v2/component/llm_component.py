from typing import Dict, Any

from workflow_v2.component.base_component import BaseComponent
from workflow_v2.workflow_logging_config import WorkflowContextLogger


class LLMComponent(BaseComponent):
    """LLM组件"""

    def __init__(self, component_id: str, title: str, node_data: Dict[str, Any], logger: WorkflowContextLogger):
        super().__init__(component_id, title, logger)
        self.llm_params = self._extract_llm_params(node_data)

    def _extract_llm_params(self, node_data: Dict[str, Any]) -> Dict[str, Any]:
        """从节点数据中提取LLM参数"""
        llm_params = {}
        if 'data' in node_data and 'inputs' in node_data['data']:
            for param in node_data['data']['inputs'].get('llmParam', []):
                name = param['name']
                value = param['input']['value'].get('content', '')
                llm_params[name] = value
        return llm_params

    async def execute(self) -> Dict[str, Any]:
        self.logger.info(f"LLMComponent {self.title} execute")
        self.logger.info(f"LLMComponent {self.title} inputs: {self.inputs}")
        return {"output": "LLM response"}
