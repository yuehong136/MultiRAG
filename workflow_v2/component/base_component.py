from abc import ABC, abstractmethod
from typing import Any

from workflow_v2.workflow_logging_config import ComponentLogger, WorkflowContextLogger


class BaseComponent(ABC):
    def __init__(self, component_id: str, title: str, logger: WorkflowContextLogger):
        self.id = component_id
        self.title = title
        self._inputs: dict[str, Any] = {}
        self._outputs: dict[str, Any] = {}
        self._nodes: dict[str, Any] = {}
        self.workflow_node: Any = None
        self.logger = ComponentLogger(logger, self)

    @property
    def inputs(self) -> dict[str, Any]:
        return self._inputs

    @inputs.setter
    def inputs(self, values: dict[str, Any]):
        self._inputs = values

    @property
    def outputs(self) -> dict[str, Any]:
        return self._outputs

    @property
    def nodes(self) -> dict[str, Any]:
        return self._nodes

    @nodes.setter
    def nodes(self, values: dict[str, Any]):
        self._nodes = values

    @abstractmethod
    async def execute(self) -> dict[str, Any]:
        """执行组件逻辑"""
        pass
