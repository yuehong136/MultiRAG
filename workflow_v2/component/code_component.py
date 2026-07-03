from typing import Any

import aiohttp

from common.settings import SCRIPT_SCHEDULER_HOST, SCRIPT_SCHEDULER_PORT
from workflow_v2.component.base_component import BaseComponent
from workflow_v2.workflow_logging_config import WorkflowContextLogger


class CodeComponent(BaseComponent):
    """代码组件"""

    def __init__(self, component_id: str, title: str, node_data: dict[str, Any], logger: WorkflowContextLogger):
        super().__init__(component_id, title, logger)
        self.code = node_data["data"]["inputs"].get("code", "")
        self.language = node_data["data"]["inputs"].get("language", 3)
        self.output_definition = node_data["data"]["outputs"]
        self.timeout = 30  # 默认超时时间

    async def execute(self) -> dict[str, Any]:
        try:
            if self.language == 3:  # Python
                # 调用脚本调度服务
                code_execute_resp = await self.run_temporary_script(self.code, self.inputs)
                if code_execute_resp.get("status") != "success":
                    self.logger.error(f"Code component execution failed: {code_execute_resp.get('message')}")
                    raise Exception(f"Code component execution failed: {code_execute_resp.get('message')}")
                original_outputs = code_execute_resp.get("data")
                return self.parse_output(self.output_definition, original_outputs)
            else:
                raise ValueError(f"Unsupported language: {self.language}")
        except Exception as e:
            self.logger.error(f"Error occurred while executing code component: {e}")
            raise e

    async def execute_alone(self, input_value: dict, batch_value: dict | None = None) -> dict[str, Any]:
        self.inputs = input_value
        return await self.execute()

    async def run_temporary_script(self, script: str, args: dict[str, Any], base_url: str = f"http://{SCRIPT_SCHEDULER_HOST}:{SCRIPT_SCHEDULER_PORT}") -> dict:
        """
        Send an asynchronous request to run a temporary script with given arguments.

        Args:
            script (str): The Python script to execute
            args (dict[str, Any]): Arguments to pass to the script
            base_url (str): Base URL of the API endpoint (default: http://localhost:8124)

        Returns:
            dict: The response from the server

        Raises:
            aiohttp.ClientError: If the request fails
        """
        # Construct the endpoint URL
        endpoint = f"{base_url}/api/v1/script-scheduler/run-temporary-script"

        # Prepare the request headers
        headers = {"Content-Type": "application/json"}

        # Prepare the request payload
        payload = {"script": script, "args": args}

        try:
            # Create a timeout for the request
            timeout = aiohttp.ClientTimeout(total=self.timeout)

            # Send POST request asynchronously
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url=endpoint, headers=headers, json=payload) as response:
                    # Raise an exception for bad status codes
                    response.raise_for_status()

                    # Return the JSON response
                    return await response.json()

        except aiohttp.ClientError as e:
            self.logger.error(f"Error making request: {e!s}")
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

            type_name = type_def.get("type", "").lower()

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
                element_schema = type_def.get("schema", {})

                # Convert each element in the list
                try:
                    if element_schema.get("type") == "object":
                        nested_schema = element_schema.get("schema", [])
                        # If the schema is empty, preserve the original elements
                        if not nested_schema:
                            return value
                        return [parse_schema_recursively(nested_schema, item) for item in value]
                    else:
                        return [convert_value(item, element_schema) for item in value]
                except (ValueError, TypeError):
                    return None
            elif type_name == "object":
                if not isinstance(value, dict):
                    return None

                nested_schema = type_def.get("schema", [])
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
            schema_map = {item["name"]: item for item in schema}

            for name, schema_item in schema_map.items():
                value = data.get(name) if isinstance(data, dict) else None
                result[name] = convert_value(value, schema_item)

            return result

        # Special case for list outputs:
        # When the output definition has a single field of type list
        # and the actual output is already a list
        if len(output_structure) == 1 and output_structure[0].get("type") == "list" and isinstance(actual_output, list):
            field_name = output_structure[0].get("name")
            return {field_name: convert_value(actual_output, output_structure[0])}

        return parse_schema_recursively(output_structure, actual_output)
