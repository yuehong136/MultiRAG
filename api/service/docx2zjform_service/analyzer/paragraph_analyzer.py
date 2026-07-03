import logging

from ..component import DescriptionComponent, InputComponent
from ..component.base import Component
from ..component.factory import ComponentFactory
from ..component.radio import RadioComponent
from ..component.sign import SignComponent
from ..constants import ComponentType
from ..element import Element, ElementType, ParagraphElement
from .analysis_context import AnalysisContext
from .base import ElementAnalyzer
from .paragraph_analyzer_util import identify_input, split_lines


class ParagraphElementAnalyzer(ElementAnalyzer):
    """段落元素分析器"""

    def can_handle(self, element: Element) -> bool:
        return element.type == ElementType.PARAGRAPH

    def analyze(self, element: ParagraphElement, context: AnalysisContext) -> list[Component]:
        logging.info(f"处理段落元素：{element.content}")
        components = []

        # 尝试按照换行符分割内容
        split_content_by_splitlines = split_lines(element.content)
        if len(split_content_by_splitlines) > 0:
            logging.info("尝试按照换行符分割内容")
            for line in split_content_by_splitlines:
                components.extend(self._analyze_content(line))
        else:
            components.extend(self._analyze_content(element.content))

        return components

    def _analyze_content(self, content: str) -> list[Component]:
        """分析内容并创建对应的组件

        Args:
            content: 要分析的文本内容

        Returns:
            List[Component]: 创建的组件列表
        """
        components = []

        if len(identify_input(content)) > 0:
            logging.info("尝试识别待填项字段")
            for input in identify_input(content):
                if "签字" in input:
                    component: SignComponent = ComponentFactory.create(ComponentType.SIGN)
                    component.set_title(input)
                    components.append(component)
                else:
                    component: InputComponent = ComponentFactory.create(ComponentType.INPUT)
                    component.set_title(input)
                    components.append(component)
        elif "□是" in content and "□否" in content:
            component: RadioComponent = ComponentFactory.create(ComponentType.RADIO)
            component.set_title(content)
            components.append(component)
        else:
            logging.info("尝试识别描述内容")
            component: DescriptionComponent = ComponentFactory.create(ComponentType.DESCRIPTION)
            component.set_content(content)
            components.append(component)

        return components
