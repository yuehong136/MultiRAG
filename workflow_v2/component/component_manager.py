from typing import Dict, Any

from workflow_v2.component.base_component import BaseComponent
from workflow_v2.component.component_factory import ComponentFactory
from workflow_v2.workflow_logging_config import WorkflowContextLogger, ComponentLogger


class ComponentManager:
    def __init__(self, logger: WorkflowContextLogger):
        self.logger = logger
        self.components: Dict[str, BaseComponent] = {}

    def create_component(self, node_data: Dict[str, Any]) -> BaseComponent:
        """创建组件实例，不再处理输入值"""
        component = ComponentFactory.create_component(node_data, self.logger)
        self.components[component.id] = component
        return component
