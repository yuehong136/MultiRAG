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

    async def execute(self) -> dict[str, Any]:
        self._extract_merge_groups(node_data=self.node_data)
        merged_values = self.get_first_non_empty_value(self.mergeGroups)
        self.workflow_node.input = {"mergeGroups": self.mergeGroups}
        return merged_values

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
