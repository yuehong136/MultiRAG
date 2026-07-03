from dataclasses import dataclass
from enum import Enum
from typing import Any

from workflow_v2.component.base_component import BaseComponent
from workflow_v2.workflow_exceptions import ErrorCode, WorkflowError
from workflow_v2.workflow_logging_config import WorkflowContextLogger


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
    nodes: list[Any]
    conditions: dict | None  # 分支的条件配置


class SelectorComponent(BaseComponent):
    """选择器组件"""

    def __init__(self, component_id: str, title: str, node_data: dict[str, Any], logger: WorkflowContextLogger):
        super().__init__(component_id, title, logger)
        self.branches_config = self._parse_branches(node_data)
        self.nodes = {}

    def _parse_branches(self, node_data: dict[str, Any]) -> list[dict]:
        """解析分支配置"""
        branches = node_data.get("data", {}).get("inputs", {}).get("branches", [])
        self.logger.debug(f"Parsed {len(branches)} branches for selector {self.id}")
        return branches

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

    def _get_value(self, input_def: dict) -> Any:
        """从输入定义中获取实际值"""
        if not input_def or "value" not in input_def:
            return None

        value_def = input_def["value"]
        if value_def["type"] == "literal":
            return value_def["content"]
        elif value_def["type"] == "ref":
            ref = value_def["content"]
            source_node = self.nodes.get(ref["blockID"])
            if source_node and source_node.is_completed and source_node.output is not None:
                output_name = ref.get("name")
                if output_name:
                    return self._get_nested_value(source_node.output, output_name)
                return source_node.output

            output_name = ref.get("name")
            if output_name:
                return self._get_nested_value(self.inputs, output_name)
        return None

    def _evaluate_condition(self, condition: dict) -> bool:
        """评估单个条件"""
        try:
            operator = OperatorType(int(condition["operator"]))

            # 获取左值
            left_value = self._get_value(condition["left"]["input"])
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
            if "right" in condition:
                right_value = self._get_value(condition["right"]["input"])
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
            self.logger.error(f"Error evaluating condition: {e!s}")
            return False

    def _evaluate_branch(self, branch: dict) -> bool:
        """评估分支条件"""
        try:
            if "condition" not in branch:
                self.logger.warning("Branch has no condition defined")
                return False

            conditions = branch["condition"]["conditions"]
            logic = LogicType(branch["condition"]["logic"])

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
            self.logger.error(f"Error evaluating branch: {e!s}")
            return False

    async def execute(self) -> dict[str, Any]:
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
                        "inputs": self.inputs,  # 传递输入到后续节点
                    }

            # 没有条件满足，使用else分支
            self.logger.info("No branch conditions met, using else branch")
            return {"selected_port": "false", "branch_index": -1, "inputs": self.inputs}

        except Exception as e:
            self.logger.error(f"Error executing selector: {e!s}")
            raise WorkflowError(message=f"Selector execution failed: {e!s}", error_code=ErrorCode.UNKNOWN_ERROR, details={"component_id": self.id, "error": str(e)})

    async def execute_alone(self, input_value: dict, batch_value: dict | None = None) -> dict[str, Any]:
        self.inputs = input_value
        return await self.execute()
