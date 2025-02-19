import logging
from typing import List

from .analysis_context import AnalysisContext
from .base import ElementAnalyzer
from .paragraph_analyzer_util import identify_input, extract_br_separated_content, split_lines
from ..component import InputComponent, DescriptionComponent
from ..component.factory import ComponentFactory
from ..constants import ComponentType
from ..component.base import Component
from ..element import Element, ElementType, ParagraphElement


class ParagraphElementAnalyzer(ElementAnalyzer):
    """段落元素分析器"""

    def can_handle(self, element: Element) -> bool:
        return element.type == ElementType.PARAGRAPH

    def analyze(self, element: ParagraphElement, context: AnalysisContext) -> List[Component]:
        logging.info(f"处理段落元素：{element.content}")
        components = []

        # 尝试按照br分割内容
        split_content_by_splitlines = split_lines(element.content)
        if len(split_content_by_splitlines) > 0:
            logging.info("尝试按照换行符分割内容")
            for line in split_content_by_splitlines:
                if len(identify_input(line)) > 0:
                    logging.info("尝试识别待填项字段")
                    for input in identify_input(line):
                        component: InputComponent = ComponentFactory.create(ComponentType.INPUT)
                        component.set_title(input)
                        components.append(component)
                else:
                    logging.info("尝试识别描述内容")
                    component: DescriptionComponent = ComponentFactory.create(ComponentType.DESCRIPTION)
                    component.set_content(line)
                    components.append(component)
        # INPUT
        elif len(identify_input(element.content)) > 0:
            logging.info("尝试识别待填项字段")
            for input in identify_input(element.content):
                # 使用 overload 的类型提示
                component: InputComponent = ComponentFactory.create(ComponentType.INPUT)
                component.set_title(input)
                components.append(component)
        # DESCRIPTION
        else:
            logging.info("尝试识别描述内容")
            component: DescriptionComponent = ComponentFactory.create(ComponentType.DESCRIPTION)
            component.set_content(element.content)
            components.append(component)

        return components
