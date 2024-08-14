from dataclasses import dataclass
from typing import Union, Optional
import json
from workflow.WorkflowEngine import WorkflowContext
from workflow.basic.Node import Node, NodeParameter, ValueTypeOfIODefinition, RefContentOfInputDefinition


@dataclass
class EndNodeInputDefinition:
    parameter_name: str
    value_type: ValueTypeOfIODefinition
    content: Union[RefContentOfInputDefinition, str]


@dataclass
class EndNodeParam(NodeParameter):
    input_definition_list: list[EndNodeInputDefinition]
    content: str


class EndNode(Node[EndNodeParam]):
    def __init__(self, node_parameter: EndNodeParam):
        self.name = "EndNode"
        super().__init__(node_parameter, "900001")

    def process(self, input_data: Optional[dict] = None, context: Optional[WorkflowContext] = None) -> dict:
        return input_data

    def validate_inputs(self):
        pass

    def get_output_schema(self):
        pass

    @staticmethod
    def decode(json: json) -> 'EndNode':
        node_json = json['node']
        node_id = node_json['id']
        component_param = node_json['data']['componentParam']

        input_definition = component_param['input_definition']
        content = component_param['content']
        end_node_param = EndNodeParam(input_definition_list=input_definition, content=content)
        return EndNode(end_node_param)
