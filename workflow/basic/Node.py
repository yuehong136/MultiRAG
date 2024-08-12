from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar, Optional

from workflow.WorkflowContext import WorkflowContext


@dataclass
class ValueTypeOfIODefinition(Enum):
    REF = "REF"
    LITERAL = "LITERAL"


@dataclass
class RefContentOfInputDefinition:
    source: str
    node_id: str
    name: str


@dataclass
class RefContentOfOutputDefinition:
    source: str
    node_id: str
    name: str


class NodeParameter(ABC):
    pass


T = TypeVar('T', bound=NodeParameter)


class Node(ABC, Generic[T]):
    def __init__(self, node_parameter: T, node_id: str):
        self.node_id = node_id
        self.node_parameter: T = node_parameter

    @abstractmethod
    def process(self, input_data: Optional[dict] = None, context: Optional[WorkflowContext] = None) -> Optional[dict]:
        pass

    @abstractmethod
    def validate_inputs(self):
        pass

    @abstractmethod
    def get_output_schema(self):
        pass
