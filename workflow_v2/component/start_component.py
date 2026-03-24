from typing import Dict, Any

from workflow_v2.component.base_component import BaseComponent


class StartComponent(BaseComponent):
    """开始节点组件"""

    async def execute(self) -> Dict[str, Any]:
        self.logger.info(f"StartComponent {self.title} execute")
        return self.inputs

    async def execute_alone(self, input_value: dict, batch_value: dict | None = None) -> Dict[str, Any]:
        self.inputs = input_value
        return await self.execute()
