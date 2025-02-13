from typing import Type, Dict, overload
from .base import Component
from .input import InputComponent
from ..constants import ComponentType
from .table import TableComponent
from .description import DescriptionComponent


class ComponentFactory:
    """组件工厂，负责创建各种类型的组件"""
    _component_types: Dict[ComponentType, Type[Component]] = {
        ComponentType.INPUT: InputComponent,
        ComponentType.TABLE: TableComponent,
        ComponentType.DESCRIPTION: DescriptionComponent
    }

    @classmethod
    def create(cls, component_type: ComponentType) -> Component:
        """创建指定类型的组件"""
        component_class = cls._component_types.get(component_type)
        if not component_class:
            raise ValueError(f"Unregistered component type: {component_type}")
        return component_class()

    @classmethod
    def register(cls, component_type: ComponentType, component_class: Type[Component]):
        """注册新的组件类型"""
        cls._component_types[component_type] = component_class
