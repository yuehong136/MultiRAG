from abc import ABC, abstractmethod

from api.service.docx2zjform_service.component import Component


class ElementAnalyzer(ABC):
    """元素分析器的抽象基类"""

    @abstractmethod
    def can_handle(self, element: dict) -> bool:
        """判断是否可以处理该元素"""
        pass

    @abstractmethod
    def analyze(self, element: dict) -> list[Component]:
        """分析元素并返回对应的组件列表"""
        pass
