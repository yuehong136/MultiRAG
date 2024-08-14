import asyncio
from dataclasses import dataclass
from typing import Optional, Union
import json

from starlette.datastructures import UploadFile

from workflow.WorkflowContext import WorkflowContext, NodeIOData
from workflow.basic.Component import Component, ComponentParameter
from workflow.basic.Node import ValueTypeOfIODefinition, VariableType


@dataclass
class FileReaderComponentInputDefinition:
    value_type: ValueTypeOfIODefinition
    content: Union[list[str], str]
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

    async def process(self, input_data: Optional[dict] = None, context: Optional[WorkflowContext] = None) -> dict:
        file_path = ""
        if context is not None:
            if self.component_parameter.input_definition.value_type == ValueTypeOfIODefinition.REF.value:
                ref_node_id = self.component_parameter.input_definition.content[0]
                ref_name = self.component_parameter.input_definition.content[1]
                file = context.get(ref_node_id).output_data.get(ref_name)

                # 判断是否为UploadFile
                if str(type(file)) == "<class 'starlette.datastructures.UploadFile'>":
                    print("file is UploadFile")
                    file_name, file_content = await read_file(file)

                    context.set(self.node_id, NodeIOData(
                        output_data={self.component_parameter.output_definition.variable_name: {"fileName": file_name,
                                                                                                "fileContent": file_content}}))

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


class FileReadError(Exception):
    """自定义异常类，用于文件读取错误"""
    pass


async def read_file(file: UploadFile):
    contents = await file.read()
    decoded_contents = contents.decode('utf-8')
    return file.filename, decoded_contents


async def read_file_object(file: UploadFile):
    async with file.file as f:
        # 使用 f 作为文件对象
        contents = await f.read()
    return contents


def get_file_info(file: UploadFile):
    return {
        "filename": file.filename,
        "content_type": file.content_type,
        "size": file.size
    }
