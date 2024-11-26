from typing import Dict, Any, List

import requests

from workflow_v2.component.base_component import BaseComponent
from workflow_v2.workflow_logging_config import WorkflowContextLogger


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
