"""
DOCX 模板标记与填充服务

功能特性:
- 文档解析：解析 DOCX 文档结构（段落、表格、合并单元格等）
- 智能识别：自动识别文档中的待填充位置（表格空单元格、下划线区域等）
- 数据填充：根据标记位置自动填充数据，生成新文档
"""

from .auto_recognizer import auto_recognize_placeholders
from .debug_helper import generate_debug_report
from .filler import fill_document, fill_document_with_tables
from .models import (
    Cell,
    DocumentElement,
    FieldValue,
    FillRequest,
    Paragraph,
    ParsedDocument,
    Placeholder,
    PlaceholderRequest,
    Row,
    Run,
    RunStyle,
    Table,
)
from .parser import parse_docx

__all__ = [
    'Cell',
    'DocumentElement',
    'FieldValue',
    'FillRequest',
    'Paragraph',
    'ParsedDocument',
    # Models
    'Placeholder',
    'PlaceholderRequest',
    'Row',
    'Run',
    'RunStyle',
    'Table',
    'auto_recognize_placeholders',
    'fill_document',
    'fill_document_with_tables',
    'generate_debug_report',
    # Functions
    'parse_docx',
]
