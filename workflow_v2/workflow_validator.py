from dataclasses import dataclass
from typing import Any


@dataclass
class ValidationIssue:
    message: str
    nodes: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "nodes": self.nodes}

    def format_message(self) -> str:
        base_message = f"{self.message}"
        if not self.nodes:
            return base_message

        node_details = []
        for node in self.nodes:
            title = node.get("data", {}).get("nodeMeta", {}).get("title", "Unknown")
            node_type = node.get("type", "Unknown")
            node_id = node.get("id", "Unknown")
            node_details.append(f"\n  - Node[{node_id}]: {title} (Type: {node_type})")

        return base_message + "".join(node_details)


class WorkflowValidator:
    def __init__(self, nodes: list[dict], edges: list[dict]):
        self.nodes = nodes
        self.edges = edges
        self.node_map = {node["id"]: node for node in nodes}
        self.adjacency_list = self._build_adjacency_list()
        self.issues: list[ValidationIssue] = []

    def _build_adjacency_list(self) -> dict[str, list[str]]:
        """构建邻接表表示的图"""
        adj_list = {node["id"]: [] for node in self.nodes}
        for edge in self.edges:
            source = edge["sourceNodeID"]
            target = edge["targetNodeID"]
            adj_list[source].append(target)
        return adj_list

    def _is_selector_edge(self, edge: dict) -> bool:
        """判断一条边是否是选择器节点的边"""
        return "sourcePortID" in edge

    def _get_selector_nodes(self) -> set[str]:
        """获取所有选择器节点的ID"""
        return {edge["sourceNodeID"] for edge in self.edges if self._is_selector_edge(edge)}

    def validate_selector_branches(self) -> None:
        """验证选择器节点的所有分支都有对应的后续节点"""
        for node in self.nodes:
            # 检查是否是选择器节点（type为8）
            if node["type"] == "8":
                node_id = node["id"]

                # 获取选择器节点的分支数量
                branches = node.get("data", {}).get("inputs", {}).get("branches", [])
                expected_ports = set()

                # 构建预期的端口ID集合
                # 添加true_0到true_{n-1}的端口，其中n是分支数量
                for i in range(len(branches)):
                    port_id = f"true_{i}" if i > 0 else "true"
                    expected_ports.add(port_id)
                # 添加false端口（对应else分支）
                expected_ports.add("false")

                # 获取实际的端口连接情况
                connected_ports = set()
                for edge in self.edges:
                    if edge["sourceNodeID"] == node_id and "sourcePortID" in edge:
                        connected_ports.add(edge["sourcePortID"])

                # 检查是否有未连接的分支
                missing_ports = expected_ports - connected_ports
                if missing_ports:
                    missing_ports_str = ", ".join(sorted(missing_ports))
                    self.issues.append(
                        ValidationIssue(
                            f"选择器节点缺少以下分支的后续节点连接: {missing_ports_str}",
                            [node],
                        )
                    )

    def validate_selector_node_connections(self) -> None:
        """验证被选择器节点连接的目标节点不能被其他非选择器节点连接"""
        # 获取所有选择器节点
        selector_nodes = self._get_selector_nodes()

        # 获取所有被选择器节点连接的目标节点
        selector_targets = set()
        for edge in self.edges:
            if self._is_selector_edge(edge):
                selector_targets.add(edge["targetNodeID"])

        # 检查这些目标节点的所有入边
        target_sources = {target: [] for target in selector_targets}
        for edge in self.edges:
            target = edge["targetNodeID"]
            if target in selector_targets:
                target_sources[target].append({"nodeID": edge["sourceNodeID"], "isSelector": self._is_selector_edge(edge)})

        # 对于每个目标节点，检查是否有非选择器节点的入边
        for target, sources in target_sources.items():
            non_selector_sources = [source["nodeID"] for source in sources if not source["isSelector"]]

            if non_selector_sources:
                # 获取相关的节点对象用于错误报告
                target_node = self.node_map[target]
                source_nodes = [self.node_map[source] for source in non_selector_sources]

                # 加入所有相关节点（目标节点和非法的源节点）
                involved_nodes = [target_node] + source_nodes
                for node in involved_nodes:
                    self.issues.append(ValidationIssue(f"Node {target} 连接到选择器节点，但也有来自非选择器节点的传入边", [node]))

    def detect_cycles(self) -> list[list[dict]]:
        """使用DFS检测图中的环，返回完整的节点对象列表"""

        def dfs(node_id: str, visited: set[str], path: set[str], current_path: list[dict]) -> list[dict] | None:
            visited.add(node_id)
            path.add(node_id)
            current_path.append(self.node_map[node_id])

            for neighbor_id in self.adjacency_list[node_id]:
                if neighbor_id in path:
                    cycle_start = next(i for i, node in enumerate(current_path) if node["id"] == neighbor_id)
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
            node_id = node["id"]
            if node_id not in visited:
                cycle = dfs(node_id, visited, set(), [])
                if cycle:
                    cycles.append(cycle)

        return cycles

    def validate_node_references(self) -> None:
        """验证节点引用的完整性"""
        for edge in self.edges:
            source = edge["sourceNodeID"]
            target = edge["targetNodeID"]

            if source not in self.node_map:
                self.issues.append(ValidationIssue(f"边引用不存在的源节点: {source}", [{"id": source, "type": "Unknown", "data": {"nodeMeta": {"title": "Missing Node"}}}]))

            if target not in self.node_map:
                self.issues.append(ValidationIssue(f"边引用不存在的目标节点: {target}", [{"id": target, "type": "Unknown", "data": {"nodeMeta": {"title": "Missing Node"}}}]))

    def validate_start_nodes(self) -> None:
        """验证起始节点"""
        in_degree = {node["id"]: 0 for node in self.nodes}
        for edge in self.edges:
            target = edge["targetNodeID"]
            in_degree[target] += 1

        start_nodes = [node for node in self.nodes if in_degree[node["id"]] == 0]
        if not start_nodes:
            self.issues.append(ValidationIssue("工作流没有起始节点（没有传入边的节点）"))

        for node in start_nodes:
            if node["type"] != "1":
                self.issues.append(ValidationIssue("Node has no incoming edges but is not a start node type", [node]))

    def validate_end_nodes(self) -> None:
        """验证结束节点"""
        out_degree = {node["id"]: 0 for node in self.nodes}
        for edge in self.edges:
            source = edge["sourceNodeID"]
            out_degree[source] += 1

        end_nodes = [node for node in self.nodes if out_degree[node["id"]] == 0]
        if not end_nodes:
            self.issues.append(ValidationIssue("工作流没有终端节点（没有传出边的节点）"))

        for node in end_nodes:
            if node["type"] != "2":
                self.issues.append(ValidationIssue("节点没有出口边，但又不是终端节点", [node]))

    def validate_isolated_nodes(self) -> None:
        """检查孤立节点"""
        for node in self.nodes:
            node_id = node["id"]
            if not any(edge["sourceNodeID"] == node_id for edge in self.edges) and not any(edge["targetNodeID"] == node_id for edge in self.edges):
                self.issues.append(ValidationIssue("孤立节点", [node]))

    def validate_all(self) -> list[ValidationIssue]:
        """执行所有验证检查"""
        # 检查环
        cycles = self.detect_cycles()
        for cycle in cycles:
            self.issues.append(ValidationIssue("检测到工作流程中的循环", cycle))

        # 执行其他验证
        self.validate_node_references()
        self.validate_start_nodes()
        self.validate_end_nodes()
        self.validate_isolated_nodes()
        self.validate_selector_node_connections()
        self.validate_selector_branches()

        return self.issues
