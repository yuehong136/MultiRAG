from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Any, List, Optional

import requests

from workflow_v2.workflow_exceptions import WorkflowError, ErrorCode
from workflow_v2.workflow_logging_config import WorkflowContextLogger, ComponentLogger


class OperatorType(Enum):
    EQUALS = 1  # ==
    NOT_EQUALS = 2  # !=
    GREATER_THAN = 3  # >
    LESS_THAN = 4  # <
    GREATER_THAN_EQUALS = 5  # >=
    LESS_THAN_EQUALS = 6  # <=
    CONTAINS = 7  # 包含
    NOT_CONTAINS = 8  # 不包含
    IS_EMPTY = 9  # 为空
    IS_NOT_EMPTY = 10  # 不为空
    STARTS_WITH = 11  # 以...开头
    ENDS_WITH = 12  # 以...结尾


class LogicType(Enum):
    AND = 2
    OR = 1


@dataclass
class Branch:
    """分支信息"""
    port_id: str  # 'true', 'true_1', 'false' 等
    nodes: List['WorkflowNode']
    conditions: Optional[Dict]  # 分支的条件配置


class BaseComponent(ABC):
    def __init__(self, component_id: str, title: str, logger: WorkflowContextLogger):
        self.id = component_id
        self.title = title
        self._inputs: Dict[str, Any] = {}
        self._outputs: Dict[str, Any] = {}
        self.logger = ComponentLogger(logger, self)

    @property
    def inputs(self) -> Dict[str, Any]:
        return self._inputs

    @inputs.setter
    def inputs(self, values: Dict[str, Any]):
        self._inputs = values

    @property
    def outputs(self) -> Dict[str, Any]:
        return self._outputs

    @abstractmethod
    async def execute(self) -> Dict[str, Any]:
        """执行组件逻辑"""
        pass


class StartComponent(BaseComponent):
    """开始节点组件"""

    async def execute(self) -> Dict[str, Any]:
        self.logger.info(f"StartComponent {self.title} execute")
        return self.inputs


class EndComponent(BaseComponent):
    """结束节点组件"""

    def __init__(self, component_id: str, title: str, node_data: Dict[str, Any], logger: WorkflowContextLogger):
        super().__init__(component_id, title, logger)
        self.terminate_plan = node_data['data']['inputs'].get('terminatePlan', 'useAnswerContent')
        self.streaming_output = node_data['data']['inputs'].get('streamingOutput', False)
        self.content_template = node_data['data']['inputs'].get('content', {}).get('value', {}).get('content', '')

    async def execute(self) -> Dict[str, Any]:
        if self.content_template:
            # TODO: 实现模板渲染逻辑
            return {"result": self.content_template}
        return self.inputs


class LLMComponent(BaseComponent):
    """LLM组件"""

    def __init__(self, component_id: str, title: str, node_data: Dict[str, Any], logger: WorkflowContextLogger):
        super().__init__(component_id, title, logger)
        self.llm_params = self._extract_llm_params(node_data)

    def _extract_llm_params(self, node_data: Dict[str, Any]) -> Dict[str, Any]:
        """从节点数据中提取LLM参数"""
        llm_params = {}
        if 'data' in node_data and 'inputs' in node_data['data']:
            for param in node_data['data']['inputs'].get('llmParam', []):
                name = param['name']
                value = param['input']['value'].get('content', '')
                llm_params[name] = value
        return llm_params

    async def execute(self) -> Dict[str, Any]:
        self.logger.info(f"LLMComponent {self.title} execute")
        self.logger.info(f"LLMComponent {self.title} inputs: {self.inputs}")
        return {"output": "LLM response"}


class CodeComponent(BaseComponent):
    """代码组件"""

    def __init__(self, component_id: str, title: str, node_data: Dict[str, Any], logger: WorkflowContextLogger):
        super().__init__(component_id, title, logger)
        self.code = node_data['data']['inputs'].get('code', '')
        self.language = node_data['data']['inputs'].get('language', 3)
        self.output_definition = node_data['data']['outputs']
        self.timeout = 30  # 默认超时时间

    async def execute(self) -> Dict[str, Any]:
        if self.language == 3:  # Python
            # 调用脚本调度服务
            code_execute_resp = self.run_temporary_script(self.code, self.inputs)
            original_outputs = code_execute_resp.get("data")
            return self.parse_output(self.output_definition, original_outputs)
        else:
            raise ValueError(f"Unsupported language: {self.language}")

    def run_temporary_script(self, script: str, args: Dict[str, Any], base_url: str = "http://localhost:8124") -> Dict:
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
        endpoint = f"{base_url}/api/v1/script-scheduler/run-temporary-script"

        # Prepare the request headers
        headers = {
            "Content-Type": "application/json"
        }

        # Prepare the request payload
        payload = {
            "script": script,
            "args": args
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


class SelectorComponent(BaseComponent):
    """选择器组件"""

    def __init__(self, component_id: str, title: str, node_data: Dict[str, Any], logger: WorkflowContextLogger):
        super().__init__(component_id, title, logger)
        self.logger = logger
        self.branches_config = self._parse_branches(node_data)
        self.nodes: List['WorkflowNode'] = []

    def _parse_branches(self, node_data: Dict[str, Any]) -> List[Dict]:
        """解析分支配置"""
        branches = node_data.get('data', {}).get('inputs', {}).get('branches', [])
        self.logger.debug(f"Parsed {len(branches)} branches for selector {self.id}")
        return branches

    def _get_value(self, input_def: Dict) -> Any:
        """从输入定义中获取实际值"""
        if not input_def or 'value' not in input_def:
            return None

        value_def = input_def['value']
        if value_def['type'] == 'literal':
            return value_def['content']
        elif value_def['type'] == 'ref':
            ref = value_def['content']
            source_node = self.nodes[ref['blockID']]
            if source_node.is_completed and source_node.output is not None:
                # 可以通过output_name来获取特定的输出字段
                output_name = ref.get('name')
                if output_name and output_name in source_node.output:
                    return source_node.output[output_name]
                else:
                    return source_node.output
        return None

    def _evaluate_condition(self, condition: Dict) -> bool:
        """评估单个条件"""
        try:
            operator = OperatorType(condition['operator'])

            # 获取左值
            left_value = self._get_value(condition['left']['input'])
            self.logger.debug(f"Left value for condition: {left_value}")

            # 对于IS_EMPTY和IS_NOT_EMPTY，不需要右值
            if operator in (OperatorType.IS_EMPTY, OperatorType.IS_NOT_EMPTY):
                if operator == OperatorType.IS_EMPTY:
                    result = left_value is None or left_value == ""
                else:
                    result = left_value is not None and left_value != ""
                self.logger.debug(f"Empty check result: {result}")
                return result

            # 获取右值（对于某些操作符，可能没有右值）
            right_value = None
            if 'right' in condition:
                right_value = self._get_value(condition['right']['input'])
            self.logger.debug(f"Right value for condition: {right_value}")

            # 执行比较
            result = False
            if operator == OperatorType.EQUALS:
                result = left_value == right_value
            elif operator == OperatorType.NOT_EQUALS:
                result = left_value != right_value
            elif operator == OperatorType.GREATER_THAN:
                result = float(left_value) > float(right_value)
            elif operator == OperatorType.LESS_THAN:
                result = float(left_value) < float(right_value)
            elif operator == OperatorType.GREATER_THAN_EQUALS:
                result = float(left_value) >= float(right_value)
            elif operator == OperatorType.LESS_THAN_EQUALS:
                result = float(left_value) <= float(right_value)
            elif operator == OperatorType.CONTAINS:
                result = str(right_value) in str(left_value) if left_value else False
            elif operator == OperatorType.NOT_CONTAINS:
                result = str(right_value) not in str(left_value) if left_value else True
            elif operator == OperatorType.STARTS_WITH:
                result = str(left_value).startswith(str(right_value))
            elif operator == OperatorType.ENDS_WITH:
                result = str(left_value).endswith(str(right_value))

            self.logger.debug(f"Condition evaluation result: {result}")
            return result

        except Exception as e:
            self.logger.error(f"Error evaluating condition: {str(e)}")
            return False

    def _evaluate_branch(self, branch: Dict) -> bool:
        """评估分支条件"""
        try:
            if 'condition' not in branch:
                self.logger.warning("Branch has no condition defined")
                return False

            conditions = branch['condition']['conditions']
            logic = LogicType(branch['condition']['logic'])

            self.logger.debug(f"Evaluating branch with {len(conditions)} conditions using {logic.name} logic")

            results = [self._evaluate_condition(cond) for cond in conditions]

            if logic == LogicType.AND:
                final_result = all(results)
            elif logic == LogicType.OR:
                final_result = any(results)
            else:
                self.logger.error(f"Unknown logic type: {logic}")
                return False

            self.logger.debug(f"Branch evaluation result: {final_result}")
            return final_result

        except Exception as e:
            self.logger.error(f"Error evaluating branch: {str(e)}")
            return False

    async def execute(self) -> Dict[str, Any]:
        """执行选择器逻辑"""
        self.logger.info(f"Evaluating conditions in Selector {self.title}")

        try:
            # 评估每个分支的条件
            for i, branch in enumerate(self.branches_config):
                self.logger.debug(f"Evaluating branch {i}")
                if self._evaluate_branch(branch):
                    # 根据分支索引确定对应的port_id
                    port_id = "true" if i == 0 else f"true_{i}"
                    self.logger.info(f"Branch conditions met for port {port_id}")
                    return {
                        "selected_port": port_id,
                        "branch_index": i,
                        "inputs": self.inputs  # 传递输入到后续节点
                    }

            # 没有条件满足，使用else分支
            self.logger.info("No branch conditions met, using else branch")
            return {
                "selected_port": "false",
                "branch_index": -1,
                "inputs": self.inputs
            }

        except Exception as e:
            self.logger.error(f"Error executing selector: {str(e)}")
            raise WorkflowError(
                message=f"Selector execution failed: {str(e)}",
                error_code=ErrorCode.UNKNOWN_ERROR,
                details={"component_id": self.id, "error": str(e)}
            )


class ComponentFactory:
    """组件工厂类"""

    @staticmethod
    def create_component(node_data: Dict[str, Any], logger: WorkflowContextLogger) -> BaseComponent:
        """从节点数据创建对应的组件实例"""
        component_id = node_data['id']
        title = node_data['data']['nodeMeta']['title']
        node_type = node_data['type']

        if node_type == "1":
            return StartComponent(component_id, title, logger)
        elif node_type == "2":
            return EndComponent(component_id, title, node_data, logger)
        elif node_type == "3":
            return LLMComponent(component_id, title, node_data, logger)
        elif node_type == "5":
            return CodeComponent(component_id, title, node_data, logger)
        elif node_type == "8":  # 选择器类型
            return SelectorComponent(component_id, title, node_data, logger)
        else:
            raise ValueError(f"Unknown component type: {node_type}")


class ComponentManager:
    def __init__(self, logger: WorkflowContextLogger):
        self.logger = logger
        self.components: Dict[str, BaseComponent] = {}

    def create_component(self, node_data: Dict[str, Any]) -> BaseComponent:
        """创建组件实例，不再处理输入值"""
        component = ComponentFactory.create_component(node_data, self.logger)
        self.components[component.id] = component
        return component
