from enum import Enum


class ComponentType(Enum):
    """组件类型枚举"""
    INPUT = "input"
    SIGN = "sign"
    RADIO = "radio"
    TEXTAREA = "textarea"
    SUBFORM = "subform"
    DESCRIPTION = "description"
    RICH_TEXT = "rich_text"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    DATE = "date"
    NUMBER = "number"

    @classmethod
    def get_type(cls, type_str: str) -> 'ComponentType':
        """从字符串获取组件类型"""
        try:
            return cls(type_str)
        except ValueError:
            raise ValueError(f"Unknown component type: {type_str}")
