import logging

from .analyzer import AnalysisContext, ElementAnalyzer, ParagraphElementAnalyzer, TableElementAnalyzer
from .component.base import Component
from .element import Element


class DocumentProcessor:
    """文档处理器，负责整个转换流程"""

    def __init__(self):
        self.analyzers = [TableElementAnalyzer(), ParagraphElementAnalyzer()]

    def process(self, elements: list[Element]) -> list[Component]:
        """处理文档元素并返回表单组件"""
        logging.info("处理文档元素并返回表单组件")

        # 创建分析上下文
        context = AnalysisContext(elements)

        for i, element in enumerate(elements):
            logging.info(f"处理文档元素：{i + 1}/{len(elements)}")
            context.set_current_element_index(i)
            analyzer = self._find_analyzer(element)
            if analyzer:
                element_components = analyzer.analyze(element, context)
                element.form_components = element_components

        components = []
        for element in elements:
            components.extend(element.form_components)

        return components

    def _find_analyzer(self, element: Element) -> ElementAnalyzer | None:
        """找到适合处理当前元素的分析器"""
        for analyzer in self.analyzers:
            if analyzer.can_handle(element):
                return analyzer
        return None

    def _post_process_components(self, components: list[Component]) -> list[Component]:
        """对组件进行后处理"""
        return components
