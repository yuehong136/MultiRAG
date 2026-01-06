# coding=utf-8
"""
DOCX 模板标记数据模型
"""

from typing import Literal
from pydantic import BaseModel


class RunStyle(BaseModel):
    """Run 的样式信息"""
    bold: bool = False
    italic: bool = False
    underline: bool = False
    font_size: float | None = None  # pt
    font_name: str | None = None
    color: str | None = None  # hex color


class Run(BaseModel):
    """文本片段"""
    path: str
    text: str
    style: RunStyle


class Paragraph(BaseModel):
    """段落"""
    path: str
    runs: list[Run]
    alignment: str | None = None  # left, center, right, justify


class Cell(BaseModel):
    """表格单元格"""
    path: str
    paragraphs: list[Paragraph]
    row_span: int = 1
    col_span: int = 1
    is_merged_origin: bool = True  # 是否是合并区域的起始单元格
    merge_origin_path: str | None = None  # 如果被合并，指向起始单元格的 path


class Row(BaseModel):
    """表格行"""
    path: str
    cells: list[Cell]


class Table(BaseModel):
    """表格"""
    path: str
    rows: list[Row]


class DocumentElement(BaseModel):
    """文档块级元素"""
    type: Literal["paragraph", "table"]
    path: str
    paragraph: Paragraph | None = None
    table: Table | None = None


class ParsedDocument(BaseModel):
    """解析后的文档"""
    filename: str
    elements: list[DocumentElement]


PlaceholderType = Literal["cell", "summary", "table", "dynamic_table"]


class Placeholder(BaseModel):
    """待填项"""
    path: str
    label: str
    field_key: str  # 自动生成的唯一标识，如 $1, $2, $3...
    type: PlaceholderType = "cell"
    prompt_for_ai: str = ""
    custom_fields: dict[str, str] = {}  # 用户自定义的 key-value 字段


class PlaceholderRequest(BaseModel):
    """添加/更新待填项的请求"""
    path: str
    label: str
    field_key: str | None = None  # 可选，如果不提供则自动生成
    type: PlaceholderType = "cell"
    prompt_for_ai: str = ""
    custom_fields: dict[str, str] = {}  # 用户自定义的 key-value 字段


class FieldValue(BaseModel):
    """单个字段的值"""
    id: str  # 字段 ID，如 $1, $2
    value: str  # 填充的值
    custom_fields: dict[str, str] = {}  # 用户自定义的 key-value 字段


class FillRequest(BaseModel):
    """填充数据的请求"""
    fields: list[FieldValue]  # 数组格式：[{"id": "$1", "value": "..."}, ...]
