"""
XLSX 模板标记与填充服务

功能特性:
- 文档解析：openpyxl 只读解析工作簿结构（多 sheet、合并、样式、下拉校验）
- 智能识别：启发式识别待填单元格（右填充/下填充 + 填充色收敛）与
  表格区域（连续多列表头 + 下方成片空行 -> dynamic_table）
- 数据填充：XML 直改写入（只动目标 sheet XML，形状/图表等全部原样保留），
  支持动态表格插行（合并区/校验区/绘图锚点联动平移），写后自校验
"""

from .auto_recognizer import auto_recognize_placeholders
from .filler import fill_workbook
from .models import (
    CellStyle,
    FieldValue,
    ParsedWorkbook,
    Placeholder,
    Sheet,
    SheetCell,
    SheetRow,
    TableColumn,
    TableConfig,
)
from .parser import parse_xlsx

__all__ = [
    "CellStyle",
    "FieldValue",
    "ParsedWorkbook",
    "Placeholder",
    "Sheet",
    "SheetCell",
    "SheetRow",
    "TableColumn",
    "TableConfig",
    "auto_recognize_placeholders",
    "fill_workbook",
    "parse_xlsx",
]
