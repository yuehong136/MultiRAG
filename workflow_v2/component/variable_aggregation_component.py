from typing import Any
from dataclasses import dataclass
from workflow_v2.component.base_component import BaseComponent
from workflow_v2.utils import match_parameters
from workflow_v2.workflow_logging_config import WorkflowContextLogger


@dataclass
class Variable:
    name: str
    value: Any


@dataclass
class MergeGroup:
    name: str
    variables: list[Variable]


class VariableAggregationComponent(BaseComponent):
    """变量聚合组件"""

    def __init__(self, component_id: str, title: str, node_data: dict[str, Any],
                 logger: WorkflowContextLogger):
        super().__init__(component_id, title, logger)
        self.node_data = node_data
        self.mergeGroups: list[MergeGroup] = []

    def _extract_merge_groups(self, node_data: dict[str, Any]):
        self.mergeGroups = []
        merge_groups_params = node_data['data']['inputs'].get('mergeGroups', [])
        for merge_group in merge_groups_params:
            merge_group_name = merge_group['name']
            variables = []
            inputs = []
            for i in range(len(merge_group['variables'])):
                variable = merge_group['variables'][i]
                inputs.append({
                    "name": merge_group_name + f"_{i}",
                    "input": variable
                })
            var_value_dict = match_parameters(inputs, self.nodes)
            sorted_items = sorted(var_value_dict.items(), key=lambda x: int(x[0].split('_')[-1]))
            for key, value in sorted_items:
                variables.append(Variable(name=key, value=value))
            self.mergeGroups.append(MergeGroup(name=merge_group_name, variables=variables))

    def _get_nested_value(self, data: Any, path: str) -> Any:
        parts = path.split(".")
        current = data
        for part in parts:
            if isinstance(current, dict):
                if part not in current:
                    raise KeyError(f"Cannot find path {path}")
                current = current[part]
            elif isinstance(current, list) and current and isinstance(current[0], dict):
                if part not in current[0]:
                    raise KeyError(f"Cannot find path {path}")
                current = current[0][part]
            else:
                raise KeyError(f"Cannot find path {path}")
        return current

    def _resolve_variable_value(self, variable: dict[str, Any], input_value: dict[str, Any]) -> Any:
        value_def = variable.get("value", {})
        if value_def.get("type") == "literal":
            return value_def.get("content")
        if value_def.get("type") == "ref":
            ref_content = value_def.get("content", {})
            ref_name = ref_content.get("name")
            if ref_name:
                return self._get_nested_value(input_value, ref_name)
        return None

    async def execute(self) -> dict[str, Any]:
        self._extract_merge_groups(node_data=self.node_data)
        merged_values = self.get_first_non_empty_value(self.mergeGroups)
        self.workflow_node.input = {"mergeGroups": self.mergeGroups}
        return merged_values

    async def execute_alone(self, input_value: dict, batch_value: dict | None = None) -> dict[str, Any]:
        self.mergeGroups = []
        merge_groups_params = self.node_data['data']['inputs'].get('mergeGroups', [])
        for merge_group in merge_groups_params:
            variables = []
            for i, variable in enumerate(merge_group.get('variables', [])):
                variables.append(
                    Variable(
                        name=merge_group['name'] + f"_{i}",
                        value=self._resolve_variable_value(variable, input_value),
                    )
                )
            self.mergeGroups.append(MergeGroup(name=merge_group['name'], variables=variables))

        self.workflow_node.input = {"mergeGroups": self.mergeGroups}
        return self.get_first_non_empty_value(self.mergeGroups)

    def get_first_non_empty_value(self, merge_groups):
        result = {}

        for group in merge_groups:
            # 遍历每个Variable，找到第一个非空的值
            for var in group.variables:
                # 检查值是否为非空（非空字典或非空字符串）
                if var.value and (isinstance(var.value, dict) and var.value or isinstance(var.value, str)):
                    result[group.name] = var.value
                    break  # 找到第一个非空值后就跳出内层循环

            # 如果没有找到非空值，可以设置一个默认值（可选）
            if group.name not in result:
                result[group.name] = {}

        return result
