from dataclasses import dataclass
from typing import Optional, Union
import json
from workflow.WorkflowContext import WorkflowContext, NodeIOData
from workflow.basic.Component import Component, ComponentParameter
from workflow.basic.Node import ValueTypeOfIODefinition, RefContentOfInputDefinition, VariableType


@dataclass
class FileReaderComponentInputDefinition:
    value_type: ValueTypeOfIODefinition
    content: Union[RefContentOfInputDefinition, str]
    parameter_name: str = "INPUT"


@dataclass
class FileReaderComponentOutputDefinition:
    variable_name: str = "OUTPUT"
    variable_type: VariableType = VariableType.OBJECT.value
    description: Optional[str] = None
    schema: Optional['FileReaderComponentOutputDefinition'] = None


@dataclass
class FileReaderComponentParam(ComponentParameter):
    output_definition: FileReaderComponentOutputDefinition
    input_definition: FileReaderComponentInputDefinition


class FileReaderComponent(Component[FileReaderComponentParam]):
    def __init__(self, component_parameter: FileReaderComponentParam, node_id: str):
        self.name = "FileReaderComponent"
        self.node_id = node_id
        self.component_parameter: FileReaderComponentParam = component_parameter
        super().__init__(component_parameter, node_id, self.name)

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

    @staticmethod
    def decode(json: json) -> 'FileReaderComponent':
        node_json = json['node']
        node_id = node_json['id']
        component_param = node_json['data']['componentParam']
        input_definition = component_param['input_definition']
        parameter_name = input_definition['parameter_name']
        value_type = input_definition['value_type']
        content = input_definition['content']
        FileReaderComponentInputDefinition(value_type=value_type, content=content, parameter_name=parameter_name)

        output_definition = component_param['output_definition']
        variable_name = output_definition['variable_name']
        variable_type = output_definition['variable_type']
        description = output_definition['description']
        schema = output_definition['schema']

        FileReaderComponentOutputDefinition(variable_name=variable_name, variable_type=variable_type,
                                            description=description, schema=schema)

        return FileReaderComponent(FileReaderComponentParam(
            FileReaderComponentOutputDefinition(variable_name=variable_name, variable_type=variable_type,
                                                description=description, schema=schema),
            FileReaderComponentInputDefinition(value_type=value_type, content=content, parameter_name=parameter_name)),
            node_id)
