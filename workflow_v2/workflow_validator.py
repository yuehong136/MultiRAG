from enum import Enum
from typing import Dict, List, Set, Optional, Any
from dataclasses import dataclass


class ValidationLevel(Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationIssue:
    level: ValidationLevel
    message: str
    nodes: Optional[List[Dict[str, Any]]] = None  # 存储完整的节点对象而不是仅仅存储ID

    def format_message(self) -> str:
        """格式化验证信息，包含详细的节点信息"""
        base_message = f"{self.level.value.upper()}: {self.message}"
        if not self.nodes:
            return base_message

        # 添加节点详细信息
        node_details = []
        for node in self.nodes:
            title = node.get('data', {}).get('nodeMeta', {}).get('title', 'Unknown')
            node_type = node.get('type', 'Unknown')
            node_id = node.get('id', 'Unknown')
            node_details.append(f"\n  - Node[{node_id}]: {title} (Type: {node_type})")

        return base_message + ''.join(node_details)


class WorkflowValidator:
    def __init__(self, nodes: List[Dict], edges: List[Dict]):
        self.nodes = nodes
        self.edges = edges
        self.node_map = {node['id']: node for node in nodes}
        self.adjacency_list = self._build_adjacency_list()
        self.issues: List[ValidationIssue] = []

    def _build_adjacency_list(self) -> Dict[str, List[str]]:
        """构建邻接表表示的图"""
        adj_list = {node['id']: [] for node in self.nodes}
        for edge in self.edges:
            source = edge['sourceNodeID']
            target = edge['targetNodeID']
            adj_list[source].append(target)
        return adj_list

    def detect_cycles(self) -> List[List[Dict]]:
        """使用DFS检测图中的环，返回完整的节点对象列表"""

        def dfs(node_id: str, visited: Set[str], path: Set[str], current_path: List[Dict]) -> Optional[List[Dict]]:
            visited.add(node_id)
            path.add(node_id)
            current_path.append(self.node_map[node_id])

            for neighbor_id in self.adjacency_list[node_id]:
                if neighbor_id in path:
                    # 找到环，返回环中的完整节点对象
                    cycle_start = next(i for i, node in enumerate(current_path)
                                       if node['id'] == neighbor_id)
                    return current_path[cycle_start:]
                if neighbor_id not in visited:
                    cycle = dfs(neighbor_id, visited, path, current_path)
                    if cycle:
                        return cycle

            path.remove(node_id)
            current_path.pop()
            return None

        visited = set()
        cycles = []

        for node in self.nodes:
            node_id = node['id']
            if node_id not in visited:
                cycle = dfs(node_id, visited, set(), [])
                if cycle:
                    cycles.append(cycle)

        return cycles

    def validate_node_references(self) -> None:
        """验证节点引用的完整性"""
        for edge in self.edges:
            source = edge['sourceNodeID']
            target = edge['targetNodeID']

            if source not in self.node_map:
                self.issues.append(ValidationIssue(
                    ValidationLevel.ERROR,
                    f"Edge references non-existent source node: {source}",
                    [{'id': source, 'type': 'Unknown', 'data': {'nodeMeta': {'title': 'Missing Node'}}}]
                ))

            if target not in self.node_map:
                self.issues.append(ValidationIssue(
                    ValidationLevel.ERROR,
                    f"Edge references non-existent target node: {target}",
                    [{'id': target, 'type': 'Unknown', 'data': {'nodeMeta': {'title': 'Missing Node'}}}]
                ))

    def validate_start_nodes(self) -> None:
        """验证起始节点"""
        in_degree = {node['id']: 0 for node in self.nodes}
        for edge in self.edges:
            target = edge['targetNodeID']
            in_degree[target] += 1

        start_nodes = [node for node in self.nodes if in_degree[node['id']] == 0]
        if not start_nodes:
            self.issues.append(ValidationIssue(
                ValidationLevel.ERROR,
                "Workflow has no start nodes (nodes with no incoming edges)"
            ))

        for node in start_nodes:
            if node['type'] != '1':
                self.issues.append(ValidationIssue(
                    ValidationLevel.WARNING,
                    "Node has no incoming edges but is not a start node type",
                    [node]
                ))

    def validate_end_nodes(self) -> None:
        """验证结束节点"""
        out_degree = {node['id']: 0 for node in self.nodes}
        for edge in self.edges:
            source = edge['sourceNodeID']
            out_degree[source] += 1

        end_nodes = [node for node in self.nodes if out_degree[node['id']] == 0]
        if not end_nodes:
            self.issues.append(ValidationIssue(
                ValidationLevel.ERROR,
                "Workflow has no end nodes (nodes with no outgoing edges)"
            ))

        for node in end_nodes:
            if node['type'] != '2':
                self.issues.append(ValidationIssue(
                    ValidationLevel.WARNING,
                    "Node has no outgoing edges but is not an end node type",
                    [node]
                ))

    def validate_isolated_nodes(self) -> None:
        """检查孤立节点"""
        for node in self.nodes:
            node_id = node['id']
            if (not any(edge['sourceNodeID'] == node_id for edge in self.edges) and
                    not any(edge['targetNodeID'] == node_id for edge in self.edges)):
                self.issues.append(ValidationIssue(
                    ValidationLevel.ERROR,
                    "Node is isolated (no incoming or outgoing edges)",
                    [node]
                ))

    def validate_all(self) -> List[ValidationIssue]:
        """执行所有验证检查"""
        # 检查环
        cycles = self.detect_cycles()
        for cycle in cycles:
            self.issues.append(ValidationIssue(
                ValidationLevel.ERROR,
                "Detected cycle in workflow",
                cycle
            ))

        # 执行其他验证
        self.validate_node_references()
        self.validate_start_nodes()
        self.validate_end_nodes()
        self.validate_isolated_nodes()

        return self.issues
