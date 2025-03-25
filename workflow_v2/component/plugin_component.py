import copy
from typing import Any

# 替换 requests 为 aiohttp
import aiohttp
from aiohttp import ClientTimeout

from api.settings import SCRIPT_SCHEDULER_PORT, SCRIPT_SCHEDULER_HOST
from workflow_v2.component.base_component import BaseComponent
from workflow_v2.utils import dict_arrays_to_array_dicts, match_parameters, map_schema_with_values
from workflow_v2.workflow_logging_config import WorkflowContextLogger
from dataclasses import dataclass


@dataclass
class BatchConfig:
    """批处理配置类"""
    batch_enable: bool = False
    batch_size: int = 100
    concurrent_size: int = 10
    input_lists: list[dict[str, Any]] = None

    @classmethod
    def from_batch_config(cls, config: dict[str, Any]) -> 'BatchConfig':
        """从批处理配置创建实例"""
        if not config:
            return cls()

        input_lists = config.get('inputLists', [])
        if not isinstance(input_lists, list):
            input_lists = [input_lists]

        return cls(
            batch_enable=config.get('batchEnable', False),
            batch_size=config.get('batchSize', 100),
            concurrent_size=config.get('concurrentSize', 10),
            input_lists=input_lists
        )


class PluginComponent(BaseComponent):
    """代码组件"""

    def __init__(self, component_id: str, title: str, node_data: dict[str, Any], logger: WorkflowContextLogger):
        super().__init__(component_id, title, logger)
        self.batch_config: BatchConfig = self._extract_batch_config(node_data)
        self.plugin_info = node_data['data']['inputs'].get('pluginInfo', {})
        self.output_definition = node_data['data']['outputs']
        self.timeout = 30  # 默认超时时间
        # 添加 session 属性以便复用 HTTP 连接
        self._session = None

    def _extract_batch_config(self, node_data: dict[str, Any]) -> BatchConfig:
        """从节点数据中提取批处理配置"""
        batch_data = node_data['data']['inputs'].get('batch', {})
        return BatchConfig.from_batch_config(batch_data)

    async def execute(self) -> dict[str, Any]:
        # 创建一个可复用的 aiohttp session
        async with aiohttp.ClientSession() as session:
            self._session = session

            if self.batch_config.batch_enable:
                input_value_dict_list = []

                batch_param_list = dict_arrays_to_array_dicts(
                    match_parameters(self.batch_config.input_lists, self.nodes))
                for batch_param_value in batch_param_list:
                    temp_nodes = copy.deepcopy(self.nodes)
                    temp_node_output = temp_nodes.get(self.workflow_node.id).output
                    if temp_node_output:
                        temp_nodes.get(self.workflow_node.id).output.update(batch_param_value)
                    else:
                        temp_nodes.get(self.workflow_node.id).output = batch_param_value
                    result = match_parameters(self.workflow_node.input_schema, temp_nodes)
                    input_value_dict_list.append(result)

                self.workflow_node.input = input_value_dict_list

                # 使用 asyncio 并发执行而不是 ThreadPoolExecutor
                import asyncio
                from asyncio import Semaphore

                # 创建信号量限制并发数
                sem = Semaphore(self.batch_config.concurrent_size)

                async def execute_single_plugin_async(input_value: dict[str, Any]) -> dict[str, Any]:
                    async with sem:  # 使用信号量控制并发
                        code_execute_resp = await self.run_plugin_script(
                            self.plugin_info.get('script', ''),
                            input_value,
                            self.plugin_info.get('pluginId', '')
                        )
                        if code_execute_resp.get('status') != 'success':
                            self.logger.error(f"Plugin code execution failed: {code_execute_resp.get('message')}")
                            raise Exception(f"Plugin code execution failed: {code_execute_resp.get('message')}")

                        # 对于批量执行，我们需要解析单个输出的结构
                        list_schema = next((item for item in self.output_definition if item['name'] == 'outputList'),
                                           None)
                        if list_schema and list_schema.get('type') == 'list':
                            single_output_schema = list_schema.get('schema', {}).get('schema', [])
                            return self.parse_output(single_output_schema, code_execute_resp.get("data"))
                        return {}

                # 并发执行所有任务并保持原始顺序
                tasks = [execute_single_plugin_async(input_value) for input_value in input_value_dict_list]
                parsed_outputs = await asyncio.gather(*tasks)

                # 返回正确的输出格式
                return {"outputList": parsed_outputs}
            else:
                code_execute_resp = await self.run_plugin_script(
                    self.plugin_info.get('script', ''),
                    self.inputs,
                    self.plugin_info.get('pluginId', '')
                )
                if code_execute_resp.get('status') != 'success':
                    self.logger.error(f"Plugin code execution failed: {code_execute_resp.get('message')}")
                    raise Exception(f"Plugin code execution failed: {code_execute_resp.get('message')}")
                original_outputs = code_execute_resp.get("data")
                return self.parse_output(self.output_definition, original_outputs)

    async def execute_alone(self, input_value: dict, batch_value: dict | None = None) -> dict:
        """Execute plugin component in standalone mode

        Args:
            input_value: Input parameters for single execution
            batch_value: Batch parameters for batch execution

        Returns:
            dict containing execution results
        """
        self.logger.info(f"PluginComponent {self.title} execute")
        self.logger.info(f"PluginComponent {self.title} inputs: {input_value}")

        # 创建一个可复用的 aiohttp session
        async with aiohttp.ClientSession() as session:
            self._session = session

            if self.batch_config.batch_enable:
                # 使用辅助函数生成批量输入参数列表
                input_value_dict_list = map_schema_with_values(self.workflow_node.input_schema, input_value,
                                                               batch_value)
                self.inputs = input_value_dict_list

                # 使用 asyncio 并发执行
                import asyncio
                from asyncio import Semaphore

                # 创建信号量限制并发数
                sem = Semaphore(self.batch_config.concurrent_size)

                async def execute_single_plugin_async(input_value: dict[str, Any]) -> dict[str, Any]:
                    async with sem:  # 使用信号量控制并发
                        code_execute_resp = await self.run_plugin_script(
                            self.plugin_info.get('script', ''),
                            input_value,
                            self.plugin_info.get('pluginId', '')
                        )
                        if code_execute_resp.get('status') != 'success':
                            self.logger.error(f"Plugin code execution failed: {code_execute_resp.get('message')}")
                            raise Exception(f"Plugin code execution failed: {code_execute_resp.get('message')}")

                        # 对于批量执行，我们需要解析单个输出的结构
                        list_schema = next((item for item in self.output_definition if item['name'] == 'outputList'),
                                           None)
                        if list_schema and list_schema.get('type') == 'list':
                            single_output_schema = list_schema.get('schema', {}).get('schema', [])
                            return self.parse_output(single_output_schema, code_execute_resp.get("data"))
                        return {}

                # 并发执行所有任务
                tasks = [execute_single_plugin_async(input_value) for input_value in input_value_dict_list]
                parsed_outputs = await asyncio.gather(*tasks)

                # 返回正确的输出格式
                return {"outputList": parsed_outputs}
            else:
                # 单次执行模式
                self.inputs = input_value
                code_execute_resp = await self.run_plugin_script(
                    self.plugin_info.get('script', ''),
                    self.inputs,
                    self.plugin_info.get('pluginId', '')
                )
                if code_execute_resp.get('status') != 'success':
                    self.logger.error(f"Plugin code execution failed: {code_execute_resp.get('message')}")
                    raise Exception(f"Plugin code execution failed: {code_execute_resp.get('message')}")
                original_outputs = code_execute_resp.get("data")
                return self.parse_output(self.output_definition, original_outputs)

    async def run_plugin_script(self, script: str, args: dict[str, Any], plugin_id: str,
                                base_url: str = f"http://{SCRIPT_SCHEDULER_HOST}:{SCRIPT_SCHEDULER_PORT}") -> dict:
        """
        异步发送请求执行临时脚本并传入参数。

        Args:
            script (str): 要执行的Python脚本
            args (dict[str, Any]): 传递给脚本的参数
            plugin_id (str): 插件ID
            base_url (str): API端点的基础URL (默认: http://localhost:8124)

        Returns:
            dict: 服务器的响应

        Raises:
            aiohttp.ClientError: 如果请求失败
        """
        # 构建端点URL
        endpoint = f"{base_url}/api/v1/script-scheduler/run-plugin-script"

        # 准备请求头
        headers = {
            "Content-Type": "application/json"
        }

        # 准备请求载荷
        payload = {
            "script": script,
            "args": args,
            "plugin_id": str(plugin_id)
        }

        try:
            # 创建请求超时
            timeout = ClientTimeout(total=self.timeout)

            # 发送POST请求
            async with self._session.post(
                    url=endpoint,
                    headers=headers,
                    json=payload,
                    timeout=timeout
            ) as response:
                # 检查状态码
                response.raise_for_status()

                # 返回JSON响应
                return await response.json()

        except aiohttp.ClientError as e:
            self.logger.error(f"Error making request: {str(e)}")
            raise

    def parse_output(self, output_structure: list[dict], actual_output: Any) -> dict:
        """
        Parse the actual output according to the defined output structure.

        Args:
            output_structure (list[dict]): The structure definition of the expected output
            actual_output (Any): The actual output from the script execution

        Returns:
            dict: The parsed output conforming to the defined structure
        """

        def convert_value(value: Any, type_def: dict) -> Any:
            """
            Convert value according to the specified type definition.

            Args:
                value: The value to convert
                type_def: The type definition, either a string or a dict with schema

            Returns:
                The converted value
            """
            if value is None:
                return None

            type_name = type_def.get('type', '').lower()

            # Handle basic types
            if type_name == "string":
                return str(value)
            elif type_name == "integer":
                try:
                    return int(float(value))
                except (ValueError, TypeError):
                    return None
            elif type_name == "boolean":
                return bool(value)
            elif type_name == "float":
                try:
                    return float(value)
                except (ValueError, TypeError):
                    return None
            elif type_name == "list":
                if not isinstance(value, list):
                    return None

                # Get schema for list elements
                element_schema = type_def.get('schema', {})

                # Convert each element in the list
                try:
                    if element_schema.get('type') == 'object':
                        nested_schema = element_schema.get('schema', [])
                        # If the schema is empty, preserve the original elements
                        if not nested_schema:
                            return value
                        return [parse_schema_recursively(nested_schema, item)
                                for item in value]
                    else:
                        return [convert_value(item, element_schema) for item in value]
                except (ValueError, TypeError):
                    return None
            elif type_name == "object":
                if not isinstance(value, dict):
                    return None

                nested_schema = type_def.get('schema', [])
                # If the schema is empty, preserve the original object
                if not nested_schema:
                    return value
                return parse_schema_recursively(nested_schema, value)

            return value

        def parse_schema_recursively(schema: list[dict], data: dict) -> dict:
            """
            Recursively parse the schema and data.

            Args:
                schema (list[dict]): Schema definition
                data (dict): Actual data to parse

            Returns:
                dict: Parsed data according to schema
            """
            result = {}
            schema_map = {item['name']: item for item in schema}

            for name, schema_item in schema_map.items():
                value = data.get(name) if isinstance(data, dict) else None
                result[name] = convert_value(value, schema_item)

            return result

        # Special case for list outputs:
        # When the output definition has a single field of type list
        # and the actual output is already a list
        if (len(output_structure) == 1 and
                output_structure[0].get('type') == 'list' and
                isinstance(actual_output, list)):
            field_name = output_structure[0].get('name')
            return {field_name: convert_value(actual_output, output_structure[0])}

        return parse_schema_recursively(output_structure, actual_output)
