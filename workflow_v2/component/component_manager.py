from typing import Any

from workflow_v2.component.base_component import BaseComponent
from workflow_v2.component.component_factory import ComponentFactory
from workflow_v2.workflow_logging_config import WorkflowContextLogger


class ComponentManager:
    def __init__(self, logger: WorkflowContextLogger | None = None, **kwargs):
        self.logger = logger
        self.components: dict[str, BaseComponent] = {}

        self.db = kwargs.get('db', None)
        self.user = kwargs.get('user', None)

    def create_component(self, node_data: dict[str, Any]) -> BaseComponent:
        """创建组件实例，不再处理输入值"""
        component = ComponentFactory.create_component(node_data, self.logger, db=self.db, user=self.user)
        self.components[component.id] = component
        return component
