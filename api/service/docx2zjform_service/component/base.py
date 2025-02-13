import json
from abc import abstractmethod
from copy import deepcopy
from typing import Dict, Any
from ..constants import ComponentType
from ..utils.generate_code import generate_code


class Component:
    """表单组件基类"""

    def __init__(self, component_type: ComponentType):
        self.type = component_type
        self.original_json = self._get_original_json()
        self.json_data = self._generate_component()

    @abstractmethod
    def _get_original_json(self) -> Dict[str, Any]:
        """获取原始JSON数据"""
        pass

    def _generate_component(self) -> Dict[str, Any]:
        """生成组件数据"""
        new_json = deepcopy(self.original_json)
        new_json["id"] = self.type.value.upper() + "_" + generate_code().upper()
        return new_json

    def to_dict(self) -> Dict:
        """将组件转换为字典形式"""
        result = {
            "type": self.type.value,
        }
        return result
