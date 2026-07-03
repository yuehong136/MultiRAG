import json
from dataclasses import dataclass
from urllib.parse import unquote

from core.utils.minio_conn import MultiRAGMinio
from workflow.basic.Component import Component, ComponentParameter
from workflow.basic.Node import VariableType
from workflow.utils.MinioOperator import MinioOperator
from workflow.WorkflowContext import NodeIOData, WorkflowContext


@dataclass
class MinIOSelectionComponentOutputDefinition:
    variable_name: str = "FILE_PATH"
    variable_type: str = VariableType.STRING.value


@dataclass
class MinIOSelectionComponentParam(ComponentParameter):
    output_definition: MinIOSelectionComponentOutputDefinition


class MinIOSelectionComponent(Component[MinIOSelectionComponentParam]):
    def __init__(self, component_parameter: MinIOSelectionComponentParam, node_id: str):
        self.name = "MinIOSelectionComponent"
        self.node_id = node_id
        self.component_parameter: MinIOSelectionComponentParam = component_parameter
        super().__init__(component_parameter, node_id, self.name)

    async def process(self, input_data: dict | None = None, context: WorkflowContext | None = None,
                      **kwargs) -> dict:
        minio_path = input_data['input_data']
        print(f"minio_path = {minio_path}")
        bucket_name, object_name = parse_minio_path(minio_path)
        minio_operator = MinioOperator()
        file_info_list = minio_operator.list_objects(bucket_name=bucket_name, prefix=object_name, recursive=False)
        file_list = []
        for file_info in file_info_list:
            file_obj = {}
            file_name = file_info['name']
            file_obj['file_name'] = file_name
            if (file_name.endswith(".txt") or
                    file_name.endswith(".docx") or
                    file_name.endswith(".pdf")):
                    # file_name.endswith(".doc")):
                file = minio_operator.download_to_memory(bucket_name=bucket_name, object_name=file_name)
                file_obj['file_content'] = file
                file_list.append(file_obj)
        context.set(str(self.node_id),
                    NodeIOData(output_data={self.component_parameter.output_definition.variable_name: file_list}))

    def validate_inputs(self):
        pass

    def get_output_schema(self):
        pass

    @staticmethod
    def decode(json: json) -> 'MinIOSelectionComponent':
        node_json = json['node']
        node_id = node_json['id']
        component_param = node_json['data']['componentParam']
        variable_name = component_param['output_definition']['variable_name']
        variable_type = component_param['output_definition']['variable_type']
        return MinIOSelectionComponent(
            MinIOSelectionComponentParam(MinIOSelectionComponentOutputDefinition(variable_name, variable_type)),
            node_id)

    @staticmethod
    def get_files_in_directory(client, bucket_name, directory_prefix, load_content=False):
        """
        获取指定 MinIO "目录"（前缀）下的所有文件，可选择性地加载文件内容。

        参数:
        client (Minio): MinIO 客户端实例
        bucket_name (str): 桶的名称
        directory_prefix (str): 要列出内容的"目录"前缀
        load_content (bool): 是否加载文件内容到内存

        返回:
        list: 包含文件信息的字典列表，每个字典包含文件名和可选的文件内容
        """
        if not directory_prefix.endswith('/'):
            directory_prefix += '/'

        files = []

        try:
            objects = client.list_objects(bucket_name, prefix=directory_prefix, recursive=False)

            for obj in objects:
                object_name = unquote(obj.object_name)

                if not object_name.endswith('/'):
                    file_name = object_name.split('/')[-1]
                    if not file_name.endswith(".txt"):
                        continue
                    file_info = {"name": file_name}
                    print(f"file_name: {file_name}")

                    if load_content:
                        try:
                            response = client.get_object(bucket_name, object_name)
                            file_content = response.read()
                            file_info["content"] = file_content
                        except Exception as e:
                            print(f"Error loading content for {file_name}: {e}")
                            file_info["content"] = None
                        finally:
                            response.close()
                            response.release_conn()

                    files.append(file_info)

        except Exception as e:
            print(f"Error listing objects: {e}")

        return files


import os


def get_files_from_folder(folder_path):
    """
    读取指定文件夹下的所有文件，并返回打开的文件对象列表。
    注意：使用后需要手动关闭文件。

    :param folder_path: 要读取的文件夹路径
    :return: 包含打开的文件对象的列表
    """
    file_list = []

    if not os.path.exists(folder_path):
        raise FileNotFoundError(f"文件夹路径不存在: {folder_path}")

    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)
        if os.path.isfile(file_path):
            try:
                file = open(file_path, 'rb')
                file_list.append(file)
            except Exception as e:
                print(f"打开文件 {filename} 时发生错误: {e!s}")

    return file_list


def parse_minio_path(minio_path):
    bucket_name = None
    object_name = None
    if minio_path.startswith("/"):
        minio_path = minio_path[1:]
        bucket_name = minio_path.split('/')[0]
        object_name = '/'.join(minio_path.split('/')[1:])
    else:
        bucket_name = minio_path.split('/')[0]
        object_name = '/'.join(minio_path.split('/')[1:])
    return bucket_name, object_name


if __name__ == "__main__":
    client = MultiRAGMinio()
    MinIOSelectionComponent.get_files_in_directory(client.conn, "2024", "/", load_content=False)
