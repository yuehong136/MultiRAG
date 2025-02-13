from typing import List, Dict
from .base import ElementAnalyzer
from ..component import InputComponent, DescriptionComponent
from ..component.factory import ComponentFactory
from ..constants import ComponentType
from ..component.base import Component
from ..element import Element, ElementType, ParagraphElement


class ParagraphElementAnalyzer(ElementAnalyzer):
    """段落元素分析器"""

    def can_handle(self, element: Element) -> bool:
        return element.type == ElementType.PARAGRAPH

    def analyze(self, element: ParagraphElement) -> List[Component]:
        components = []

        # INPUT
        if len(self._identify_input(element.content)) > 0:
            for input in self._identify_input(element.content):
                # 使用 overload 的类型提示
                component: InputComponent = ComponentFactory.create(ComponentType.INPUT)
                component.set_title(input)
                components.append(component)
        # DESCRIPTION
        else:
            component: DescriptionComponent = ComponentFactory.create(ComponentType.DESCRIPTION)
            component.set_content(element.content)
            components.append(component)

        return components

    def _identify_input(self, text: str) -> list:
        """
        提取字符串中的待填项字段（以冒号结尾的部分）

        Args:
            text (str): 输入的字符串

        Returns:
            list: 待填项字段列表
        """
        import re

        # 修改正则表达式模式：
        # ([^：:]+) 匹配除冒号外的任何字符（一个或多个）
        # (?:：|:) 匹配中文或英文冒号（非捕获组）
        pattern = r'([^：:]+)(?:：|:)'

        # 查找所有匹配项
        matches = re.findall(pattern, text)

        # 处理匹配结果：去除首尾空格
        return [match.strip() for match in matches]
