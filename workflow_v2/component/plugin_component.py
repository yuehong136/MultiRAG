import copy
from typing import Dict, Any, List, Optional

import requests

from workflow_v2.component.base_component import BaseComponent
from workflow_v2.utils import dict_arrays_to_array_dicts, match_parameters, map_schema_with_values
from workflow_v2.workflow_logging_config import WorkflowContextLogger
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor


@dataclass
class BatchConfig:
    """批处理配置类"""
    batch_enable: bool = False
    batch_size: int = 100
    concurrent_size: int = 10
    input_lists: List[Dict[str, Any]] = None

    @classmethod
    def from_batch_config(cls, config: Dict[str, Any]) -> 'BatchConfig':
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

    def __init__(self, component_id: str, title: str, node_data: Dict[str, Any], logger: WorkflowContextLogger):
        super().__init__(component_id, title, logger)
        self.batch_config: BatchConfig = self._extract_batch_config(node_data)
        self.plugin_info = node_data['data']['inputs'].get('pluginInfo', {})
        self.output_definition = node_data['data']['outputs']
        self.timeout = 30  # 默认超时时间

    def _extract_batch_config(self, node_data: Dict[str, Any]) -> BatchConfig:
        """从节点数据中提取批处理配置"""
        batch_data = node_data['data']['inputs'].get('batch', {})
        return BatchConfig.from_batch_config(batch_data)

    async def execute(self) -> Dict[str, Any]:
        if self.batch_config.batch_enable:
            input_value_dict_list = []

            batch_param_list = dict_arrays_to_array_dicts(match_parameters(self.batch_config.input_lists, self.nodes))
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

            with ThreadPoolExecutor(max_workers=10) as executor:
                def execute_single_plugin(input_value: Dict[str, Any]) -> Dict[str, Any]:
                    code_execute_resp = self.run_plugin_script(
                        self.plugin_info.get('script', ''),
                        input_value,
                        self.plugin_info.get('pluginId', '')
                    )
                    if code_execute_resp.get('status') != 'success':
                        self.logger.error(f"Plugin code execution failed: {code_execute_resp.get('message')}")
                        raise Exception(f"Plugin code execution failed: {code_execute_resp.get('message')}")

                    # 对于批量执行，我们需要解析单个输出的结构
                    # 从 output_definition 中获取实际的单个输出的 schema
                    list_schema = next((item for item in self.output_definition if item['name'] == 'outputList'), None)
                    if list_schema and list_schema.get('type') == 'list':
                        single_output_schema = list_schema.get('schema', {}).get('schema', [])
                        return self.parse_output(single_output_schema, code_execute_resp.get("data"))
                    return {}

                # 使用 list 保持原始顺序
                parsed_outputs = list(executor.map(execute_single_plugin, input_value_dict_list))

            # 返回正确的输出格式
            return {"outputList": parsed_outputs}
        else:
            code_execute_resp = self.run_plugin_script(self.plugin_info.get('script', ''), self.inputs,
                                                       self.plugin_info.get('pluginId', ''))
            if code_execute_resp.get('status') != 'success':
                self.logger.error(f"Plugin code execution failed: {code_execute_resp.get('message')}")
                raise Exception(f"Plugin code execution failed: {code_execute_resp.get('message')}")
            original_outputs = code_execute_resp.get("data")
            return self.parse_output(self.output_definition, original_outputs)

    async def execute_alone(self, input_value: dict, batch_value: Optional[dict] = None) -> dict:
        """Execute plugin component in standalone mode

        Args:
            input_value: Input parameters for single execution
            batch_value: Batch parameters for batch execution

        Returns:
            Dict containing execution results
        """
        self.logger.info(f"PluginComponent {self.title} execute")
        self.logger.info(f"PluginComponent {self.title} inputs: {input_value}")

        if self.batch_config.batch_enable:
            # 使用辅助函数生成批量输入参数列表
            input_value_dict_list = map_schema_with_values(self.workflow_node.input_schema, input_value, batch_value)
            self.inputs = input_value_dict_list

            with ThreadPoolExecutor(max_workers=10) as executor:
                def execute_single_plugin(input_value: Dict[str, Any]) -> Dict[str, Any]:
                    code_execute_resp = self.run_plugin_script(
                        self.plugin_info.get('script', ''),
                        input_value,
                        self.plugin_info.get('pluginId', '')
                    )
                    if code_execute_resp.get('status') != 'success':
                        self.logger.error(f"Plugin code execution failed: {code_execute_resp.get('message')}")
                        raise Exception(f"Plugin code execution failed: {code_execute_resp.get('message')}")

                    # 对于批量执行，我们需要解析单个输出的结构
                    list_schema = next((item for item in self.output_definition if item['name'] == 'outputList'), None)
                    if list_schema and list_schema.get('type') == 'list':
                        single_output_schema = list_schema.get('schema', {}).get('schema', [])
                        return self.parse_output(single_output_schema, code_execute_resp.get("data"))
                    return {}

                # 使用 list 保持原始顺序
                parsed_outputs = list(executor.map(execute_single_plugin, input_value_dict_list))

            # 返回正确的输出格式
            return {"outputList": parsed_outputs}
        else:
            # 单次执行模式
            self.inputs = input_value
            code_execute_resp = self.run_plugin_script(
                self.plugin_info.get('script', ''),
                self.inputs,
                self.plugin_info.get('pluginId', '')
            )
            if code_execute_resp.get('status') != 'success':
                self.logger.error(f"Plugin code execution failed: {code_execute_resp.get('message')}")
                raise Exception(f"Plugin code execution failed: {code_execute_resp.get('message')}")
            original_outputs = code_execute_resp.get("data")
            return self.parse_output(self.output_definition, original_outputs)

    def run_plugin_script(self, script: str, args: Dict[str, Any], plugin_id: str,
                          base_url: str = "http://localhost:8124") -> Dict:
        """
        Send a request to run a temporary script with given arguments.

        Args:
            script (str): The Python script to execute
            args (Dict[str, Any]): Arguments to pass to the script
            base_url (str): Base URL of the API endpoint (default: http://localhost:8124)

        Returns:
            Dict: The response from the server

        Raises:
            requests.exceptions.RequestException: If the request fails
        """
        # Construct the endpoint URL
        endpoint = f"{base_url}/api/v1/script-scheduler/run-plugin-script"

        # Prepare the request headers
        headers = {
            "Content-Type": "application/json"
        }

        # Prepare the request payload
        payload = {
            "script": script,
            "args": args,
            "plugin_id": str(plugin_id)
        }

        try:
            # Send POST request
            response = requests.post(
                url=endpoint,
                headers=headers,
                json=payload
            )

            # Raise an exception for bad status codes
            response.raise_for_status()

            # Return the JSON response
            return response.json()

        except requests.exceptions.RequestException as e:
            print(f"Error making request: {str(e)}")
            raise

    def parse_output(self, output_structure: List[Dict], actual_output: Dict) -> Dict:
        """
        Parse the actual output according to the defined output structure.

        Args:
            output_structure (List[Dict]): The structure definition of the expected output
            actual_output (Dict): The actual output from the script execution

        Returns:
            Dict: The parsed output conforming to the defined structure
        """

        def convert_value(value: Any, type_def: Dict) -> Any:
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
                        return [parse_schema_recursively(element_schema.get('schema', []), item)
                                for item in value]
                    else:
                        return [convert_value(item, element_schema) for item in value]
                except (ValueError, TypeError):
                    return None

            return value

        def parse_schema_recursively(schema: List[Dict], data: Dict) -> Dict:
            """
            Recursively parse the schema and data.

            Args:
                schema (List[Dict]): Schema definition
                data (Dict): Actual data to parse

            Returns:
                Dict: Parsed data according to schema
            """
            result = {}
            schema_map = {item['name']: item for item in schema}

            for name, schema_item in schema_map.items():
                value = data.get(name)
                type_name = schema_item.get('type', '').lower()

                try:
                    if type_name == "object":
                        if value is None:
                            result[name] = None
                        else:
                            nested_schema = schema_item.get('schema', [])
                            result[name] = parse_schema_recursively(nested_schema, value)
                    else:
                        result[name] = convert_value(value, schema_item)

                except (ValueError, TypeError):
                    result[name] = None

            return result

        return parse_schema_recursively(output_structure, actual_output)
