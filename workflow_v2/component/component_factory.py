from typing import Dict, Any

from workflow_v2.component.base_component import BaseComponent
from workflow_v2.component.code_component import CodeComponent
from workflow_v2.component.end_component import EndComponent
from workflow_v2.component.llm_component import LLMComponent
from workflow_v2.component.selector_component import SelectorComponent
from workflow_v2.component.start_component import StartComponent
from workflow_v2.workflow_logging_config import WorkflowContextLogger


class ComponentFactory:
    """组件工厂类"""

    @staticmethod
    def create_component(node_data: Dict[str, Any], logger: WorkflowContextLogger) -> BaseComponent:
        """从节点数据创建对应的组件实例"""
        component_id = node_data['id']
        title = node_data['data']['nodeMeta']['title']
        node_type = node_data['type']

        if node_type == "1":
            return StartComponent(component_id, title, logger)
        elif node_type == "2":
            return EndComponent(component_id, title, node_data, logger)
        elif node_type == "3":
            return LLMComponent(component_id, title, node_data, logger)
        elif node_type == "5":
            return CodeComponent(component_id, title, node_data, logger)
        elif node_type == "8":  # 选择器类型
            return SelectorComponent(component_id, title, node_data, logger)
        else:
            raise ValueError(f"Unknown component type: {node_type}")
