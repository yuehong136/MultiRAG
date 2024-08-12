from dataclasses import dataclass


@dataclass
class Edge:
    source_node_id: str
    target_node_id: str
