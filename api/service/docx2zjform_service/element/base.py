from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List

from api.service.docx2zjform_service.component import Component


class ElementType(Enum):
    """元素类型枚举"""
    PARAGRAPH = "paragraph"
    TABLE = "table"

    @classmethod
    def get_type(cls, type_str: str) -> 'ElementType':
        try:
            return cls(type_str)
        except ValueError:
            raise ValueError(f"Unknown element type: {type_str}")


class Element(ABC):
    """文档元素基类"""

    def __init__(self, element_type: ElementType):
        self.type = element_type
        self.form_components: List[Component] = []

    @abstractmethod
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        pass

    @classmethod
    @abstractmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Element':
        """从字典创建元素"""
        pass
