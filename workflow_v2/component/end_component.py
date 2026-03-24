from typing import Dict, Any

from workflow_v2.component.base_component import BaseComponent
from workflow_v2.utils import parse_template
from workflow_v2.workflow_logging_config import WorkflowContextLogger


class EndComponent(BaseComponent):
    """结束节点组件"""

    def __init__(self, component_id: str, title: str, node_data: Dict[str, Any], logger: WorkflowContextLogger):
        super().__init__(component_id, title, logger)
        self.terminate_plan = node_data['data']['inputs'].get('terminatePlan', 'useAnswerContent')
        self.streaming_output = node_data['data']['inputs'].get('streamingOutput', False)
        self.content_template = node_data['data']['inputs'].get('content', {}).get('value', {}).get('content', '')

    async def execute(self) -> Dict[str, Any]:
        return {"output": parse_template(self.content_template, self.inputs)}

    async def execute_alone(self, input_value: dict, batch_value: dict | None = None) -> Dict[str, Any]:
        self.inputs = input_value
        return await self.execute()
