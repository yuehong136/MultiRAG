from abc import ABC, abstractmethod
from typing import Dict, Any

from workflow_v2.workflow_logging_config import WorkflowContextLogger, ComponentLogger


class BaseComponent(ABC):
    def __init__(self, component_id: str, title: str, logger: WorkflowContextLogger):
        self.id = component_id
        self.title = title
        self._inputs: Dict[str, Any] = {}
        self._outputs: Dict[str, Any] = {}
        self.logger = ComponentLogger(logger, self)

    @property
    def inputs(self) -> Dict[str, Any]:
        return self._inputs

    @inputs.setter
    def inputs(self, values: Dict[str, Any]):
        self._inputs = values

    @property
    def outputs(self) -> Dict[str, Any]:
        return self._outputs

    @abstractmethod
    async def execute(self) -> Dict[str, Any]:
        """执行组件逻辑"""
        pass
