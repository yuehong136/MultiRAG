"""
XLSX 模板标记数据模型

path 坐标系与 docx_marker 风格一致：sheet[n]/row[m]/cell[k]
- n: 工作表索引（workbook 内顺序，0-based）
- m: 行索引（0-based，对应 Excel 行号 m+1）
- k: 列索引（0-based，对应 Excel 列 k+1，如 k=2 -> C 列）

Placeholder / FieldValue 与 docx_marker 保持同构，便于前端组件与中台链路复用。
"""

from typing import Literal

from pydantic import BaseModel


class CellStyle(BaseModel):
    """单元格样式（供前端渲染，非完整样式模型）"""

    fill_color: str | None = None  # ARGB hex（仅直接 RGB 填充；主题色暂不解析）
    bold: bool = False
    font_size: float | None = None
    font_name: str | None = None
    font_color: str | None = None
    align_h: str | None = None  # left / center / right
    align_v: str | None = None  # top / center / bottom
    wrap_text: bool = False
    number_format: str | None = None


class SheetCell(BaseModel):
    """单元格"""

    path: str
    value: str = ""  # 显示值（公式单元格为缓存计算值）
    row_span: int = 1
    col_span: int = 1
    is_merged_origin: bool = True  # 是否是合并区域的起始单元格
    merge_origin_path: str | None = None  # 如果被合并，指向起始单元格的 path
    style: CellStyle = CellStyle()
    has_formula: bool = False  # 公式单元格禁止标记
    validation_options: list[str] | None = None  # 下拉列表选项（data validation type=list）


class SheetRow(BaseModel):
    """行"""

    path: str
    height: float | None = None
    cells: list[SheetCell]


class Sheet(BaseModel):
    """工作表"""

    path: str  # sheet[n]
    name: str
    index: int
    max_row: int
    max_col: int
    col_widths: list[float | None] = []  # 按列序，None 表示默认宽度
    rows: list[SheetRow]


class ParsedWorkbook(BaseModel):
    """解析后的工作簿"""

    filename: str
    sheets: list[Sheet]


PlaceholderType = Literal["cell", "summary", "table", "dynamic_table"]


class TableColumn(BaseModel):
    """表格列配置（cell_index 为工作表绝对列索引，0-based）"""

    cell_index: int
    name: str
    prompt_for_ai: str = ""


class TableConfig(BaseModel):
    """表格类型待填项的配置（行号均为工作表绝对行索引，0-based）"""

    dynamic: bool = False  # 动态表格：数据行数超出预留区时自动插行
    header_row: int
    data_start_row: int
    data_end_row: int  # 预留数据区最后一行
    columns: list[TableColumn]
    category_columns: list[int] = []  # 预留（xlsx 首版不支持分类列）


class Placeholder(BaseModel):
    """待填项（与 docx_marker.Placeholder 同构）"""

    path: str
    label: str
    field_key: str  # 自动生成的唯一标识，如 $1, $2, $3...
    type: PlaceholderType = "cell"
    prompt_for_ai: str = ""
    custom_fields: dict[str, str] = {}
    table_config: TableConfig | None = None
    bind: str = ""
    validation_options: list[str] | None = None  # 下拉约束，拼 LLM 提示词用


class FieldValue(BaseModel):
    """单个字段的填充值（与 docx_marker.FieldValue 同构）"""

    id: str  # 对应 Placeholder.field_key
    value: str = ""
    custom_fields: dict[str, str] = {}
    rows: list[dict[str, str]] | None = None  # 表格数据（列名 -> 值）
