from typing import List, Dict
from .base import Component
from ..constants import ComponentType


class TableComponent(Component):
    """表格组件"""

    def __init__(self):
        super().__init__(ComponentType.TABLE)

    def set_columns(self, columns: List[Dict]):
        self.set_property("columns", columns)
        return self

    def set_data(self, data: List[Dict]):
        self.set_property("data", data)
        return self
