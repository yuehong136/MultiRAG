from dataclasses import dataclass

from workflow.basic.Node import Node, NodeParameter
from workflow.WorkflowContext import WorkflowContext


@dataclass
class StartNodeInputDefinition:
    variable_name: str
    variable_type: str
    required: bool
    description: str | None = None
    schema: 'StartNodeInputDefinition' = None


@dataclass
class StartNodeParam(NodeParameter):
    input_definition_list: list[StartNodeInputDefinition]


class StartNode(Node[StartNodeParam]):
    def __init__(self, node_parameter: StartNodeParam):
        print("init startNode")
        # StartNode 只有一个输入参数，因此输出参数可以直接使用 StartNodeParam 作为输出参数
        self.output_definition_list = node_parameter.input_definition_list
        super().__init__(node_parameter, "100001")

    def process(self, input_data: dict | None = None, context: WorkflowContext | None = None, **kwargs) -> dict:
        return input_data

    def validate_inputs(self):
        pass

    def get_output_schema(self):
        pass

    def decode(self, json: str) -> 'StartNode':
        return self


if __name__ == "__main__":
    inputde1 = StartNodeInputDefinition(variable_name="BOT_USER_INPUT", variable_type="string",
                                        description="用户本轮对话输入内容", required=False)
    inputde2 = StartNodeInputDefinition(variable_name="INPUT", variable_type="string", description="测试input",
                                        required=True)
    input_definition = [inputde1, inputde2]

    node_param = StartNodeParam(input_definition_list=input_definition)
    start_node = StartNode(node_param)

    try:
        start_node.process({"BOT_USER_INPUT": "你好", "INPUT": "测试数据"})
    except ValueError as e:
        print("执行失败:", str(e))
