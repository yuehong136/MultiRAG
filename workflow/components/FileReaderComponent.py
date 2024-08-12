from dataclasses import dataclass
from typing import Optional, Union

from workflow.WorkflowContext import WorkflowContext, NodeIOData
from workflow.basic.Component import Component, ComponentParameter
from workflow.basic.Node import ValueTypeOfIODefinition, RefContentOfInputDefinition


@dataclass
class FileReaderComponentInputDefinition:
    value_type: ValueTypeOfIODefinition
    content: Union[RefContentOfInputDefinition, str]
    parameter_name: str = "FILE_PATH"


@dataclass
class FileReaderComponentOutputDefinition:
    variable_name: str


@dataclass
class FileReaderComponentParam(ComponentParameter):
    output_definition: FileReaderComponentOutputDefinition
    input_definition: FileReaderComponentInputDefinition


class FileReaderComponent(Component[FileReaderComponentParam]):
    def __init__(self, component_parameter: FileReaderComponentParam, node_id: str):
        self.node_id = node_id
        self.component_parameter: FileReaderComponentParam = component_parameter
        super().__init__(component_parameter, node_id)

    def process(self, input_data: Optional[dict] = None, context: Optional[WorkflowContext] = None) -> dict:
        file_path = ""
        if context is not None:
            if self.component_parameter.input_definition.value_type == ValueTypeOfIODefinition.REF:
                file_path = context.get(
                    self.component_parameter.input_definition.content.node_id).output_data.get(
                    self.component_parameter.input_definition.content.name)

            file_content = ""
            try:
                with open(file_path, 'r', encoding='utf-8') as file:
                    file_content = file.read()
                    context.set(self.node_id, NodeIOData(
                        output_data={self.component_parameter.output_definition.variable_name: file_content}))
            except FileNotFoundError:
                print(f"错误: 文件 '{file_path}' 未找到。")
                return None
            except IOError:
                print(f"错误: 无法读取文件 '{file_path}'。")
                return None

    def validate_inputs(self):
        pass

    def get_output_schema(self):
        pass
