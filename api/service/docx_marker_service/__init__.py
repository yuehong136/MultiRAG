# coding=utf-8
"""
DOCX 模板标记与填充服务

功能特性:
- 文档解析：解析 DOCX 文档结构（段落、表格、合并单元格等）
- 智能识别：自动识别文档中的待填充位置（表格空单元格、下划线区域等）
- 数据填充：根据标记位置自动填充数据，生成新文档
"""

from .models import (
    Placeholder,
    PlaceholderRequest,
    FieldValue,
    FillRequest,
    ParsedDocument,
    DocumentElement,
    Paragraph,
    Run,
    RunStyle,
    Table,
    Row,
    Cell,
)

from .parser import parse_docx
from .auto_recognizer import auto_recognize_placeholders
from .filler import fill_document
from .debug_helper import generate_debug_report

__all__ = [
    # Models
    'Placeholder',
    'PlaceholderRequest',
    'FieldValue',
    'FillRequest',
    'ParsedDocument',
    'DocumentElement',
    'Paragraph',
    'Run',
    'RunStyle',
    'Table',
    'Row',
    'Cell',
    # Functions
    'parse_docx',
    'auto_recognize_placeholders',
    'fill_document',
    'generate_debug_report',
]
