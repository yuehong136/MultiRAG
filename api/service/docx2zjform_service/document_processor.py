from typing import List
from .analyzer import ElementAnalyzer, TableElementAnalyzer, ParagraphElementAnalyzer
from .component.base import Component
from .element import Element


class DocumentProcessor:
    """文档处理器，负责整个转换流程"""

    def __init__(self):
        self.analyzers = [
            TableElementAnalyzer(),
            ParagraphElementAnalyzer()
        ]

    def process(self, elements: List[Element]) -> List[Component]:
        """处理文档元素并返回表单组件"""
        components = []

        for element in elements:
            analyzer = self._find_analyzer(element)
            if analyzer:
                element_components = analyzer.analyze(element)
                components.extend(element_components)

        return self._post_process_components(components)

    def _find_analyzer(self, element: Element) -> ElementAnalyzer | None:
        """找到适合处理当前元素的分析器"""
        for analyzer in self.analyzers:
            if analyzer.can_handle(element):
                return analyzer
        return None

    def _post_process_components(self, components: List[Component]) -> List[Component]:
        """对组件进行后处理"""
        return components
