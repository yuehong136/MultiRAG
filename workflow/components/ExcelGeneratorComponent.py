import os
import uuid
from dataclasses import dataclass
from typing import Union, Optional, List
import json

import pyexcel

from core import settings
from workflow.WorkflowContext import WorkflowContext, NodeIOData
from workflow.basic.Component import Component, ComponentParameter
from workflow.basic.Node import ValueTypeOfIODefinition, Batch
from workflow.llm.VolcengineLLM import VolcengineLLM
from jsonpath_ng import jsonpath, parse

from workflow.utils import string_cipher
from workflow.utils.MinioOperator import MinioOperator


@dataclass
class ExcelGeneratorComponentInputDefinition:
    parameter_name: str
    value_type: ValueTypeOfIODefinition
    content: Union[list[str], str]
    schema: Optional['ExcelGeneratorComponentInputDefinition'] = None


@dataclass
class ExcelGeneratorComponentOutputDefinition:
    variable_name: str
    variable_type: Optional[str] = None
    description: Optional[str] = None


@dataclass
class ExcelGeneratorComponentParam(ComponentParameter):
    output_definition: Optional[ExcelGeneratorComponentOutputDefinition] = None
    input_definition_list: Optional[list[ExcelGeneratorComponentInputDefinition]] = None
    batch: Optional[List[Batch]] = None


class ExcelGeneratorComponent(Component[ExcelGeneratorComponentParam]):
    def __init__(self, component_parameter: ExcelGeneratorComponentParam, node_id: str):
        self.name = "ExcelGeneratorComponent"
        self.node_id = node_id
        self.component_parameter = component_parameter
        super().__init__(component_parameter, node_id)

    async def process(self, input_data: Optional[dict] = None, context: Optional[WorkflowContext] = None,
                      **kwargs) -> dict:
        headers = []
        parameter_dict = {}
        for input_definition in self.node_parameter.input_definition_list:
            parameter_name = input_definition['parameter_name']
            headers.append(parameter_name)
            if input_definition['value_type'] == ValueTypeOfIODefinition.REF.value:
                ref_node_id = input_definition['content'][0]
                ref_name = input_definition['content'][1]
                ref_node_data = context.get(str(ref_node_id)).output_data
                parsed = parse('$.' + ref_name)
                actual_ref_value = parsed.find(ref_node_data)[0].value
                input_value: list[str] = []
                if type(actual_ref_value) == list:
                    for ref_value in actual_ref_value:
                        input_value.append(ref_value[input_definition['content'][2]])
                    parameter_dict[parameter_name] = input_value
                else:
                    parameter_dict[parameter_name] = actual_ref_value[input_definition['content'][2]]

            elif input_definition.value_type == ValueTypeOfIODefinition.LITERAL:
                parameter_dict[parameter_name] = input_definition.content

        data = self.transform_dict_to_list(parameter_dict, headers)
        random_file_name = str(uuid.uuid4())
        complete_file_name = random_file_name + ".xlsx"
        excel_file = create_excel_in_memory(headers, data, filename=complete_file_name)
        # 上传到MinIO
        minio_operator = MinioOperator()
        minio_operator.create_bucket(bucket_name=settings.MINIO["workflow_bucket"])
        minio_operator.upload_file_from_memory(bucket_name=settings.MINIO["workflow_bucket"],
                                               object_name=complete_file_name,
                                               file_data=excel_file[1])
        file_info = {
            "bucket_name": settings.MINIO["workflow_bucket"],
            "object_name": complete_file_name
        }
        json_str = json.dumps(file_info)
        encrypted_url = string_cipher.encrypt(json_str, "DATAV-SK-666")

        download_url = f"http://0.0.0.0:80/api/cmai/workflowManagement/workflowManagement/downloadAiWorkFlowReport/{encrypted_url}_xlsx"
        context.set(str(self.node_id),
                    NodeIOData(output_data={self.component_parameter.output_definition['variable_name']: download_url}))

    def validate_inputs(self):
        pass

    def get_output_schema(self):
        pass

    @staticmethod
    def decode(json: json) -> 'ExcelGeneratorComponent':
        node_json = json['node']
        node_id = node_json['id']
        component_param = node_json['data']['componentParam']
        input_definition = component_param['input_definition']
        output_definition = component_param['output_definition']

        llm_component_param = ExcelGeneratorComponentParam(output_definition=output_definition,
                                                           input_definition_list=input_definition)
        return ExcelGeneratorComponent(llm_component_param, node_id)

    @staticmethod
    def create_excel(headers, data, output_path):
        """
        创建一个Excel文件。

        参数:
        headers (list): 表头列表
        data (list of lists): 数据列表，每个子列表代表一行
        output_path (str): 输出Excel文件的路径

        返回:
        None
        """
        # 检查数据是否为空
        if not data:
            print("警告: 没有数据可写入。")
            return

        # 将表头和数据合并
        all_data = [headers] + data

        try:
            # 保存为Excel文件
            pyexcel.save_as(array=all_data, dest_file_name=output_path)
            print(f"Excel文件已成功创建: {output_path}")
        except Exception as e:
            print(f"创建Excel文件时发生错误: {str(e)}")

    def transform_dict_to_list(self, parameter_dict, headers):
        """
        将字典转换为指定格式的列表。

        参数:
        parameter_dict (dict): 包含文件名和内容的字典
        headers (list): 指定输出列表的列顺序

        返回:
        list: 转换后的二维列表
        """
        # 获取字典中每个键的值的长度，假设所有值的长度相同
        length = len(parameter_dict[headers[0]])

        # 初始化结果列表
        result = []

        # 遍历并配对元素
        for i in range(length):
            row = [parameter_dict[header][i] for header in headers]
            result.append(row)

        return result


import io
from openpyxl import Workbook

import io
from openpyxl import Workbook
from datetime import datetime


def create_excel_in_memory(headers, data, filename=None):
    """
    在内存中创建一个Excel文件并返回其字节串和文件名。

    参数:
    headers (list): 表头列表
    data (list of lists): 数据列表，每个子列表代表一行
    filename (str, optional): 自定义文件名。如果未提供，将生成一个默认名称。

    返回:
    tuple: (文件名, 字节串内容)
    """
    # 检查数据是否为空
    if not data:
        print("警告: 没有数据可写入。")
        return None, None

    # 如果没有提供文件名，生成一个默认的
    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"excel_report_{timestamp}.xlsx"
    elif not filename.endswith('.xlsx'):
        filename += '.xlsx'

    # 创建一个新的工作簿
    wb = Workbook()
    ws = wb.active

    # 写入表头
    ws.append(headers)

    # 写入数据
    for row in data:
        ws.append(row)

    # 将工作簿保存到内存中的字节流
    excel_buffer = io.BytesIO()
    try:
        wb.save(excel_buffer)
        # 获取字节串
        excel_bytes = excel_buffer.getvalue()
        print(f"Excel文件 '{filename}' 已成功创建在内存中")
        return filename, excel_bytes
    except Exception as e:
        print(f"创建Excel文件时发生错误: {str(e)}")
        return None, None
    finally:
        excel_buffer.close()


if __name__ == "__main__":
    # 示例表头
    headers = ["姓名", "年龄", "城市"]

    # 示例数据
    data = [
        ["张三", 25, "北京"],
        ["李四", 30, "上海"],
        ["王五", 28, "广州"]
    ]

    # 输出路径
    output_path = "output.xlsx"

    # 调用函数创建Excel文件
    ExcelGeneratorComponent.create_excel(headers, data, output_path)
