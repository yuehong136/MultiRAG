from dataclasses import dataclass
from typing import Any


@dataclass
class OutputDefinition:
    type: str
    name: str
    required: bool
    description: str

class StartComponent:
    def __init__(self):
        self.outputs: dict[str, OutputDefinition] = {}

    def add_output(self, output: OutputDefinition):
        self.outputs[output.name] = output

    def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for name, output in self.outputs.items():
            if output.required and name not in input_data:
                raise ValueError(f"Required output '{name}' is missing")
            result[name] = input_data.get(name)
        return result

class Workflow:
    def __init__(self):
        self.start_component = StartComponent()

    def define_start_outputs(self, outputs: list[OutputDefinition]):
        for output in outputs:
            self.start_component.add_output(output)

    def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return self.start_component.execute(input_data)

# 使用示例
if __name__ == "__main__":
    # 定义工作流
    workflow1 = Workflow()
    workflow1.define_start_outputs([
        OutputDefinition("string", "BOT_USER_INPUT", False, "用户本轮对话输入内容"),
        OutputDefinition("string", "INPUT", True, "测试input")
    ])

    # 另一个工作流实例
    workflow2 = Workflow()
    workflow2.define_start_outputs([
        OutputDefinition("string", "ANOTHER_INPUT", True, "另一个输入")
    ])

    # 执行工作流
    try:
        result1 = workflow1.execute({
            "BOT_USER_INPUT": "你好",
            "INPUT": "测试数据"
        })
        print("工作流1执行成功:", result1)

        result2 = workflow2.execute({
            "ANOTHER_INPUT": "其他数据"
        })
        print("工作流2执行成功:", result2)

    except ValueError as e:
        print("执行失败:", str(e))
