
from docx.document import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from ..utils.table_converter import TableConverter
from . import Element
from .paragraph import ParagraphElement
from .run import Run
from .table import TableElement


class DocumentParser:
    """文档解析器"""

    @staticmethod
    async def parse(doc: Document) -> list[Element]:
        elements = []

        for element in doc.element.body:
            if isinstance(element, CT_P):
                paragraph = Paragraph(element, doc)
                if paragraph.text.strip():  # 只处理非空段落
                    runs = []
                    for run in paragraph.runs:
                        if run.text.strip():
                            # 安全地获取字体大小，如果为 None 则保持为 None
                            font_size = run.font.size.pt if run.font.size else None

                            runs.append(Run(
                                text=run.text,
                                # 明确地将可能为 None 的值转换为 bool
                                bold=bool(run.bold) if run.bold is not None else False,
                                italic=bool(run.italic) if run.italic is not None else False,
                                underline=bool(run.underline) if run.underline is not None else False,
                                font_name=run.font.name,
                                font_size=font_size
                            ))

                    elements.append(ParagraphElement(
                        content=paragraph.text,
                        style=paragraph.style.name if paragraph.style else 'Normal',
                        alignment=paragraph.alignment,
                        runs=runs
                    ))

            elif isinstance(element, CT_Tbl):
                table = Table(element, doc)
                table_html = TableConverter.convert_to_html(table)
                content = [[cell.text for cell in row.cells] for row in table.rows]

                elements.append(TableElement(
                    content=content,
                    style=table.style.name if table.style else 'TableNormal',
                    row_count=len(table.rows),
                    column_count=len(table.columns),
                    html=table_html
                ))

        return elements
