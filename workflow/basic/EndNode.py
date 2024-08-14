from dataclasses import dataclass
from typing import Union, Optional
import json

from jsonpath_ng.ext import parse

from workflow.WorkflowEngine import WorkflowContext
from workflow.basic.Node import Node, NodeParameter, ValueTypeOfIODefinition
from workflow.utils import safe_format, safe_format_double_braces


@dataclass
class EndNodeInputDefinition:
    parameter_name: str
    value_type: ValueTypeOfIODefinition
    content: Union[list[str], str]


@dataclass
class EndNodeParam(NodeParameter):
    input_definition_list: list[EndNodeInputDefinition]
    content: str


class EndNode(Node[EndNodeParam]):
    def __init__(self, node_parameter: EndNodeParam):
        self.name = "EndNode"
        super().__init__(node_parameter, "900001")

    async def process(self, input_data: Optional[dict] = None, context: Optional[WorkflowContext] = None) -> dict:
        content = self.node_parameter.content
        parameter_dict = {}
        for input_definition in self.node_parameter.input_definition_list:
            parameter_name = input_definition['parameter_name']
            if input_definition['value_type'] == ValueTypeOfIODefinition.REF.value:
                ref_node_id = input_definition['content'][0]
                ref_name = input_definition['content'][1]
                ref_node_data = context.get(ref_node_id).output_data
                ref_node_data_json_str = json.dumps(ref_node_data, ensure_ascii=False)
                ref_value = parse('$.' + ref_name).find(json.loads(ref_node_data_json_str))
                parameter_dict[parameter_name] = ref_value[0].value
            elif input_definition.value_type == ValueTypeOfIODefinition.LITERAL:
                parameter_dict[parameter_name] = input_definition.content
        return {"output": safe_format_double_braces(content, **parameter_dict)}

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


if __name__ == "__main__":
    # 使用示例
    content = "文件：{{fileName}}\n密级：{{llmOutput1}}"
    parameter_dict = {
        "fileName": "/Users/naimehao/Desktop/军事机密.txt",
        "llmOutput": "秘密"
    }

    formatted_content = safe_format_double_braces(content, **parameter_dict)
    print(formatted_content)
