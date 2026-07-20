"""
XLSX 待填项自动识别（启发式，人工标记兜底）

单元格识别规则：
1. 右填充：空单元格（或空合并区起始格）左侧紧邻非空标签格 -> cell 待填项
2. 下填充：无左侧标签时，上方紧邻非空标签格 -> cell 待填项
3. 填充色收敛：若工作表内候选待填格存在占多数的统一填充色（如模板约定的
   淡黄色待填区），则只保留该颜色的候选，显著降低误报
4. 公式单元格一律跳过；合并区成员格跳过（只看起始格）

表格区域识别规则（与表单区的结构签名区分）：
- 表单区特征是"标签|空格"横向交替，连续非空格串长度只有 1；
- 表格区特征是一行里连续 >=MIN_TABLE_HEADER_COLS 个独立非空表头格，
  且下方有连续 >=MIN_TABLE_DATA_ROWS 行在这些列上整行为空。
命中即产出一个 dynamic_table 待填项（超出预留行时引擎插行），
主题名称取表头上一行的"标题行"（整行单一非空格，去掉"六、"类编号前缀），
表格区域内的 cell 候选全部剔除。只预留 1 行数据的表格无法与"标签在上"
的表单行区分，不做自动识别，留给人工标记。
"""

import logging
import re
from collections import Counter

from .models import ParsedWorkbook, Placeholder, Sheet, SheetCell, TableColumn, TableConfig

logger = logging.getLogger(__name__)

# 命中这些关键词的标签不作为待填项（与 docx recognizer 的排除习惯一致）
EXCLUDE_KEYWORDS = ("签字", "盖章", "√")

# 待填格颜色收敛的最低占比
COLOR_DOMINANCE = 0.6

# 表格探测：表头至少要有几个独立的非空格（防止把横跨合并的标题行当表头）
MIN_TABLE_HEADER_COLS = 3
# 表格探测：表头下方至少要有几行空数据行（1 行时与"标签在上"的表单行无法区分）
MIN_TABLE_DATA_ROWS = 2

# 标题行编号前缀："六、" "6." "（六）" 等（数字后必须跟分隔符，防止误伤"2024年…"）
_TITLE_PREFIX_RE = re.compile(r"^[（(]?[一二三四五六七八九十百0-9]+([、.．:：]|[）)])\s*")


def _normalize_label(text: str) -> str:
    """标签清洗：去空白、去尾部冒号、去括号注释"""
    label = re.sub(r"\s+", "", text)
    label = re.sub(r"[（(][^）)]*[）)]$", "", label)
    return label.rstrip(":：")


def _is_blank(cell: SheetCell) -> bool:
    return cell.value.strip() == "" and not cell.has_formula


def _candidates_for_sheet(sheet: Sheet) -> list[dict]:
    grid = sheet.rows
    candidates = []
    for r, row in enumerate(grid):
        for c, cell in enumerate(row.cells):
            if not cell.is_merged_origin or cell.has_formula or not _is_blank(cell):
                continue
            label = None
            # 右填充：左侧非空标签
            if c > 0:
                left = row.cells[c - 1]
                left_text = _normalize_label(left.value)
                if left.is_merged_origin and left_text and not any(k in left_text for k in EXCLUDE_KEYWORDS):
                    label = left_text
            # 下填充：上方非空标签
            if label is None and r > 0:
                above = grid[r - 1].cells[c]
                above_text = _normalize_label(above.value)
                if above.is_merged_origin and above_text and not any(k in above_text for k in EXCLUDE_KEYWORDS):
                    label = above_text
            if label:
                candidates.append({"cell": cell, "label": label, "row": r, "col": c})
    return candidates


# ---------- 表格区域探测 ----------


def _header_runs(row_cells: list[SheetCell], row_index: int) -> list[dict]:
    """
    找出一行里连续非空格串。合并区成员格若其起始格在同一行且非空，视为串的延续；
    纵向合并的成员格、空格、空值公式格都会打断串。
    返回 [{"col_start", "col_end", "origins": [(列索引, 表头文本), ...]}]
    """
    runs: list[dict] = []
    current: dict | None = None
    for c, cell in enumerate(row_cells):
        filled = False
        if cell.is_merged_origin:
            filled = cell.value.strip() != ""
            if filled:
                if current is None:
                    current = {"col_start": c, "col_end": c, "origins": []}
                current["origins"].append((c, cell.value.strip()))
                current["col_end"] = c
        elif cell.merge_origin_path:
            # 同行横向合并的成员格延续串；纵向合并的成员格打断
            origin_match = re.search(r"row\[(\d+)\]/cell\[(\d+)\]", cell.merge_origin_path)
            if origin_match and current is not None:
                origin_row, origin_col = int(origin_match.group(1)), int(origin_match.group(2))
                same_row_origin = origin_row == row_index and any(col == origin_col for col, _ in current["origins"])
                if same_row_origin:
                    filled = True
                    current["col_end"] = c
        if not filled and current is not None:
            runs.append(current)
            current = None
    if current is not None:
        runs.append(current)
    return runs


def _row_blank_in(row_cells: list[SheetCell], col_start: int, col_end: int) -> bool:
    return all(_is_blank(cell) for cell in row_cells[col_start : col_end + 1])


def _title_above(sheet: Sheet, header_row: int) -> str | None:
    """表头紧邻的上一行若是"标题行"（整行只有一个非空起始格），取其文本去编号前缀作主题名称"""
    if header_row == 0:
        return None
    row = sheet.rows[header_row - 1]
    texts = [c.value.strip() for c in row.cells if c.is_merged_origin and c.value.strip()]
    if len(texts) != 1:
        return None
    title = _TITLE_PREFIX_RE.sub("", texts[0]).strip()
    return title or None


def _detect_tables_for_sheet(sheet: Sheet) -> list[dict]:
    """探测表格区域，返回 [{header_row, data_start, data_end, col_start, col_end, columns, label}]"""
    grid = sheet.rows
    tables: list[dict] = []
    for r, row in enumerate(grid):
        for run in _header_runs(row.cells, r):
            origins = run["origins"]
            if len(origins) < MIN_TABLE_HEADER_COLS:
                continue
            # 各格文本全部相同的是横向重复的标题行，不是表头
            if len({text for _, text in origins}) == 1:
                continue
            data_rows = 0
            rr = r + 1
            while rr < len(grid) and _row_blank_in(grid[rr].cells, run["col_start"], run["col_end"]):
                data_rows += 1
                rr += 1
            if data_rows < MIN_TABLE_DATA_ROWS:
                continue
            tables.append(
                {
                    "header_row": r,
                    "data_start": r + 1,
                    "data_end": r + data_rows,
                    "col_start": run["col_start"],
                    "col_end": run["col_end"],
                    "columns": origins,
                    "label": _title_above(sheet, r) or f"表格{len(tables) + 1}",
                }
            )
    return tables


def _in_table_region(tables: list[dict], row: int, col: int) -> bool:
    return any(t["header_row"] <= row <= t["data_end"] and t["col_start"] <= col <= t["col_end"] for t in tables)


def auto_recognize_placeholders(workbook: ParsedWorkbook) -> list[Placeholder]:
    """基于解析结果做启发式识别，返回 cell + dynamic_table 待填项列表"""
    placeholders: list[Placeholder] = []
    counter = 0

    for sheet in workbook.sheets:
        tables = _detect_tables_for_sheet(sheet)

        candidates = _candidates_for_sheet(sheet)
        # 表格区域内的 cell 候选全部剔除（由表格待填项整体覆盖）
        if tables:
            before = len(candidates)
            candidates = [c for c in candidates if not _in_table_region(tables, c["row"], c["col"])]
            logger.info(f"[recognize] sheet[{sheet.index}] 探测到 {len(tables)} 个表格区域，剔除区域内 cell 候选: {before} -> {len(candidates)}")

        # 填充色收敛：候选里存在明显占多数的统一填充色时，只保留该颜色的候选
        colors = Counter(c["cell"].style.fill_color for c in candidates if c["cell"].style.fill_color)
        if colors:
            dominant, dominant_count = colors.most_common(1)[0]
            if dominant_count / len(candidates) >= COLOR_DOMINANCE:
                filtered = [c for c in candidates if c["cell"].style.fill_color == dominant]
                logger.info(f"[recognize] sheet[{sheet.index}] 按填充色 {dominant} 收敛: {len(candidates)} -> {len(filtered)}")
                candidates = filtered

        # 表格与单元格候选按版面位置（行、列）统一排序后编号
        items: list[tuple[int, int, str, dict]] = [(t["header_row"], t["col_start"], "table", t) for t in tables] + [(c["row"], c["col"], "cell", c) for c in candidates]
        items.sort(key=lambda x: (x[0], x[1]))

        for _row, _col, kind, payload in items:
            counter += 1
            if kind == "table":
                placeholders.append(
                    Placeholder(
                        path=f"sheet[{sheet.index}]/row[{payload['data_start']}]/cell[{payload['col_start']}]",
                        label=payload["label"],
                        field_key=f"${counter}",
                        type="dynamic_table",
                        table_config=TableConfig(
                            dynamic=True,
                            header_row=payload["header_row"],
                            data_start_row=payload["data_start"],
                            data_end_row=payload["data_end"],
                            columns=[TableColumn(cell_index=col, name=name) for col, name in payload["columns"]],
                        ),
                    )
                )
            else:
                cell = payload["cell"]
                placeholders.append(
                    Placeholder(
                        path=cell.path,
                        label=payload["label"],
                        field_key=f"${counter}",
                        type="cell",
                        validation_options=cell.validation_options,
                    )
                )

    logger.info(f"[recognize] 共识别 {len(placeholders)} 个待填项")
    return placeholders
