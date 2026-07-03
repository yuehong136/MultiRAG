from ..constants import ComponentType
from .base import Component
from .description import DescriptionComponent
from .input import InputComponent
from .radio import RadioComponent
from .sign import SignComponent
from .subform import SubFormComponent
from .textarea import TextareaComponent


class ComponentFactory:
    """组件工厂，负责创建各种类型的组件"""

    _component_types: dict[ComponentType, type[Component]] = {
        ComponentType.INPUT: InputComponent,
        ComponentType.DESCRIPTION: DescriptionComponent,
        ComponentType.SUBFORM: SubFormComponent,
        ComponentType.TEXTAREA: TextareaComponent,
        ComponentType.SIGN: SignComponent,
        ComponentType.RADIO: RadioComponent,
    }

    @classmethod
    def create(cls, component_type: ComponentType) -> Component:
        """创建指定类型的组件"""
        component_class = cls._component_types.get(component_type)
        if not component_class:
            raise ValueError(f"Unregistered component type: {component_type}")
        return component_class()

    @classmethod
    def register(cls, component_type: ComponentType, component_class: type[Component]):
        """注册新的组件类型"""
        cls._component_types[component_type] = component_class
