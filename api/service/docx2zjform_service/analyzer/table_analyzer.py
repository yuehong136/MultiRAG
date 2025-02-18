from typing import List

from .analysis_context import AnalysisContext
from .base import ElementAnalyzer
from .table_analyzer_util import is_multiple_tables_with_name, MultiTableWithNameExtractor, is_single_normal_table, \
    extract_table_headers, is_inputs_table, extract_form_inputs_table, is_single_empty_table_with_multiple_br
from ..component import ComponentFactory, DescriptionComponent, InputComponent
from ..component.base import Component
from ..component.subform import SubFormComponent
from ..component.textarea import TextareaComponent
from ..constants import ComponentType
from ..element import Element, ElementType, TableElement
import logging


class TableElementAnalyzer(ElementAnalyzer):
    """表格元素分析器"""

    def can_handle(self, element: Element) -> bool:
        return element.type == ElementType.TABLE

    def analyze(self, element: TableElement, context: AnalysisContext) -> List[Component]:
        logging.info(f"处理表格元素：{element.content}")
        components = []

        # 检测HTML表格中是否存在多个表格
        if is_multiple_tables_with_name(element.html):
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
            if is_single_empty_table_with_multiple_br(element.html):
                # 如果是空单元格，则可能是需要用户填写的多行文本，此时向上查找一个元素，查看是否为描述组件，如果是，则使用它的内容作为输入框的标题
                previous_element = context.get_previous_element()
                if previous_element:
                    element_component = previous_element.form_components[0]
                    if element_component and isinstance(element_component, DescriptionComponent):
                        content = element_component.get_content()
                        textarea_component: TextareaComponent = ComponentFactory.create(ComponentType.TEXTAREA)
                        textarea_component.set_title(content)
                        components.append(textarea_component)
                        previous_element.form_components = []
            elif is_single_normal_table(element.html):
                fields = extract_table_headers(element.html)
                subform_component: SubFormComponent = ComponentFactory.create(ComponentType.SUBFORM)
                for field in fields:
                    input_component: InputComponent = ComponentFactory.create(ComponentType.INPUT)
                    input_component.set_title(field)
                    subform_component.add_input_component(input_component=input_component)
                components.append(subform_component)
            elif is_inputs_table(element.html):
                fields = extract_form_inputs_table(element.html)
                for field in fields:
                    input_component: InputComponent = ComponentFactory.create(ComponentType.INPUT)
                    input_component.set_title(field)
                    components.append(input_component)
        return components
