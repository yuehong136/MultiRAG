from dataclasses import dataclass
from typing import Union, Optional

from workflow.WorkflowEngine import WorkflowContext
from workflow.basic.Node import Node, NodeParameter, ValueTypeOfIODefinition, RefContentOfInputDefinition


@dataclass
class EndNodeOutputDefinition:
    parameter_name: str
    value_type: ValueTypeOfIODefinition
    content: Union[RefContentOfInputDefinition, str]


@dataclass
class EndNodeParam(NodeParameter):
    output_definition_list: list[EndNodeOutputDefinition]


class EndNode(Node[EndNodeParam]):
    def __init__(self, node_parameter: EndNodeParam):
        super().__init__(node_parameter, "900001")

    def process(self, input_data: Optional[dict] = None, context: Optional[WorkflowContext] = None) -> dict:
        return input_data

    def validate_inputs(self):
        pass

    def get_output_schema(self):
        pass
