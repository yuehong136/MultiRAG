from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass
class NodeIOData:
    def __init__(self, input_data: Optional[dict] = None, output_data: Optional[dict] = None):
        self.input_data = input_data
        self.output_data = output_data


@dataclass
class WorkflowContext:
    def __init__(self):
        self.data: Dict[str, NodeIOData] = {}

    def clear(self):
        self.data.clear()

    def set(self, key: str, value: NodeIOData):
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> NodeIOData:
        return self.data.get(key, default)
