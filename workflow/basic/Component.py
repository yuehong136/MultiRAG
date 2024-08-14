from abc import ABC, abstractmethod

from typing import Generic, TypeVar, Optional, Dict, Any

from workflow.WorkflowContext import WorkflowContext
from workflow.basic.Node import NodeParameter, Node

C = TypeVar('C', bound='ComponentParameter')


class ComponentParameter(NodeParameter):
    pass


class Component(Node[C]):
    def __init__(self, component_parameter: C, node_id: str, name: str = None):
        self.name = name
        self.node_id = node_id
        self.component_parameter: C = component_parameter
        super().__init__(component_parameter, node_id)

    @abstractmethod
    async def process(self, input_data: Optional[dict] = None, context: Optional[WorkflowContext] = None) -> Optional[dict]:
        pass

    @abstractmethod
    def validate_inputs(self):
        pass

    @abstractmethod
    def get_output_schema(self):
        pass

    def get_input_from_context(self, context: Dict[str, Any], key: str, default: Any = None) -> Any:
        return context.get(f"{self.node_id}.{key}", default)
