from typing import List
from .base import ElementAnalyzer
from .table_analyzer_util import detect_multiple_tables, MultiTableWithNameExtractor, \
    identify_single_table_or_inputs_pattern
from ..component import ComponentFactory, DescriptionComponent, InputComponent
from ..component.base import Component
from ..component.subform import SubFormComponent
from ..constants import ComponentType
from ..element import Element, ElementType, TableElement
import logging


class TableElementAnalyzer(ElementAnalyzer):
    """表格元素分析器"""

    def can_handle(self, element: Element) -> bool:
        return element.type == ElementType.TABLE

    def analyze(self, element: TableElement) -> List[Component]:
        logging.info(f"处理表格元素：{element.content}")
        components = []

        # 检测HTML表格中是否存在多个表格
        if detect_multiple_tables(element.html):
            # 抽取多表信息
            extractor = MultiTableWithNameExtractor(element.html)
            tables = extractor.extract_tables()
            for table in tables:
                table_name = table['table_name']
                # 表名作为描述组件
                component: DescriptionComponent = ComponentFactory.create(ComponentType.DESCRIPTION)
                component.set_content(table_name)
                components.append(component)

                # 创建子表单组件
                subform_component: SubFormComponent = ComponentFactory.create(ComponentType.SUBFORM)
                fields = table['fields']
                for field in fields:
                    # 创建输入组件
                    input_component: InputComponent = ComponentFactory.create(ComponentType.INPUT)
                    input_component.set_title(field)
                    subform_component.add_input_component(input_component)
                components.append(subform_component)
        else:
            # 判断是多输入还是单表
            result = identify_single_table_or_inputs_pattern(element.html)
            fields = result['fields']
            if result['pattern'] == 'table':
                # 创建子表单组件
                subform_component: SubFormComponent = ComponentFactory.create(ComponentType.SUBFORM)
                for field in fields:
                    # 创建输入组件
                    input_component: InputComponent = ComponentFactory.create(ComponentType.INPUT)
                    input_component.set_title(field)
                    subform_component.add_input_component(input_component=input_component)
                components.append(subform_component)
            elif result['pattern'] == 'inputs':
                # 输入组件
                for field in fields:
                    # 创建输入组件
                    input_component: InputComponent = ComponentFactory.create(ComponentType.INPUT)
                    input_component.set_title(field)
                    components.append(input_component)

        return components
