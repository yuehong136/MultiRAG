from abc import ABC, abstractmethod

from ..component.base import Component
from ..element import Element
from .analysis_context import AnalysisContext


class ElementAnalyzer(ABC):
    """元素分析器的抽象基类"""

    @abstractmethod
    def can_handle(self, element: Element) -> bool:
        """判断是否可以处理该元素"""
        pass

    @abstractmethod
    def analyze(self, element: Element, context: AnalysisContext) -> list[Component]:
        """分析元素并返回对应的组件列表"""
        pass
