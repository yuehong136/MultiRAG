from abc import ABC, abstractmethod
from enum import Enum
from typing import Generic, TypeVar, Optional

from fastapi import Depends
from requests import Session

from api.apps import manager
from api.db.db_models import get_db
# from api.db.database import get_db
from workflow.WorkflowContext import WorkflowContext

from dataclasses import dataclass


@dataclass
class Batch:
    name: str
    ref_node_id: str
    ref_name: str

    @staticmethod
    def parse_batch(json_data: str) -> list['Batch']:
        if json_data is None:
            return []

        batch_list = []

        for item in json_data:
            name = item['name']
            value = item['value']

            if len(value) >= 2:
                ref_node_id = str(value[0])
                ref_name = str(value[1])
                batch = Batch(name=name, ref_node_id=ref_node_id, ref_name=ref_name)
                batch_list.append(batch)
            else:
                print(f"Warning: Skipping item with name '{name}' due to insufficient value elements")

        return batch_list


@dataclass
class ValueTypeOfIODefinition(Enum):
    REF = "REF"
    LITERAL = "LITERAL"


@dataclass
class RefContentOfInputDefinition:
    node_id: str
    name: str


@dataclass
class RefContentOfOutputDefinition:
    node_id: str
    name: str


@dataclass
class VariableType(Enum):
    STRING = "String"
    INTEGER = "Integer"
    BOOLEAN = "Boolean"
    NUMBER = "Number"
    OBJECT = "Object"
    ARRSTRING = "Array<String>"
    ARRINTEGER = "Array<Integer>"
    ARRBOOLEAN = "Array<Boolean>"
    ARRNUMBER = "Array<Number>"
    ARROBJECT = "Array<Object>"


class NodeParameter(ABC):
    pass


T = TypeVar('T', bound=NodeParameter)


class Node(ABC, Generic[T]):
    def __init__(self, node_parameter: T, node_id: str):
        self.node_id = node_id
        self.node_parameter: T = node_parameter

    @abstractmethod
    async def process(self, input_data: Optional[dict] = None, context: Optional[WorkflowContext] = None,
                      db: Session = Depends(get_db),
                      user=Optional[Depends(manager)]) -> Optional[
        dict]:
        pass

    @abstractmethod
    def validate_inputs(self):
        pass

    @abstractmethod
    def get_output_schema(self):
        pass
