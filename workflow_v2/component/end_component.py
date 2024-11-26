from typing import Dict, Any

from workflow_v2.component.base_component import BaseComponent
from workflow_v2.workflow_logging_config import WorkflowContextLogger


class EndComponent(BaseComponent):
    """结束节点组件"""

    def __init__(self, component_id: str, title: str, node_data: Dict[str, Any], logger: WorkflowContextLogger):
        super().__init__(component_id, title, logger)
        self.terminate_plan = node_data['data']['inputs'].get('terminatePlan', 'useAnswerContent')
        self.streaming_output = node_data['data']['inputs'].get('streamingOutput', False)
        self.content_template = node_data['data']['inputs'].get('content', {}).get('value', {}).get('content', '')

    async def execute(self) -> Dict[str, Any]:
        if self.content_template:
            # TODO: 实现模板渲染逻辑
            return {"result": self.content_template}
        return self.inputs
