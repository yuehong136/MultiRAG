import json
from copy import deepcopy
from typing import Dict, Any, ClassVar, List
from ..constants import ComponentType
from ..utils.generate_code import generate_code


class Component:
    """表单组件基类"""
    _original_json: ClassVar[Dict[str, Any]] = None

    def __init__(self, component_type: ComponentType):
        self.type = component_type
        if self._original_json is None:
            raise NotImplementedError("Subclass must set _original_json")
        self.json_data = self._generate_component()

    @classmethod
    def __init_subclass__(cls, **kwargs):
        """当子类被定义时自动调用，用于初始化子类的 _original_json"""
        super().__init_subclass__(**kwargs)
        if hasattr(cls, '_json_str'):
            cls._original_json = json.loads(cls._json_str)

    def _generate_component(self) -> Dict[str, Any]:
        """生成组件数据"""
        new_json = deepcopy(self._original_json)
        new_json["id"] = self.type.value.upper() + "_" + generate_code().upper()
        return new_json

    def to_dict(self) -> Dict:
        """将组件转换为字典形式"""
        result = {
            "type": self.type.value,
        }
        return result

    @staticmethod
    def components_to_json_string(components: List['Component']) -> str:
        """
        将组件列表转换为JSON数组字符串

        Args:
            components: Component对象列表

        Returns:
            str: JSON数组字符串
        """
        json_array = [component.json_data for component in components]
        return json.dumps(json_array, ensure_ascii=False)
