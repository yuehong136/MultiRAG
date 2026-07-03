import json
from dataclasses import dataclass

from workflow.basic.Node import Node, NodeParameter, ValueTypeOfIODefinition
from workflow.utils.utils import safe_format_double_braces
from workflow.WorkflowEngine import WorkflowContext


@dataclass
class EndNodeOutputDefinition:
    parameter_name: str
    value_type: ValueTypeOfIODefinition
    content: list[str] | str


@dataclass
class EndNodeParam(NodeParameter):
    output_definition_list: list[EndNodeOutputDefinition]
    content: str


class EndNode(Node[EndNodeParam]):
    def __init__(self, node_parameter: EndNodeParam):
        self.name = "EndNode"
        super().__init__(node_parameter, "900001")

    async def process(self, input_data: dict | None = None, context: WorkflowContext | None = None, **kwargs) -> dict:
        content = self.node_parameter.content
        parameter_dict = {}
        for output_definition in self.node_parameter.output_definition_list:
            parameter_name = output_definition['parameter_name']
            if output_definition['value_type'] == ValueTypeOfIODefinition.REF.value:
                ref_node_id = output_definition['content'][0]
                ref_name = output_definition['content'][1]
                ref_node_data = context.get(str(ref_node_id)).output_data[ref_name]
                if type(ref_node_data) is str:
                    parameter_dict[parameter_name] = ref_node_data
                else:
                    parameter_dict[parameter_name] = ref_node_data[output_definition['content'][2]]
            elif output_definition.value_type == ValueTypeOfIODefinition.LITERAL:
                parameter_dict[parameter_name] = output_definition.content
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

        output_definition = component_param['output_definition']
        content = component_param['content']
        end_node_param = EndNodeParam(output_definition_list=output_definition, content=content)
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
