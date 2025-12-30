# coding=utf-8
"""
自动识别文档中的待填充位置

识别规则：
1. 表格空单元格：右填充、下填充
2. 表格非空单元格：包含冒号且独占整行
3. 段落下划线：标签 + 冒号 + 下划线区域
"""

import re
from docx import Document
from docx.table import Table as DocxTable

from .models import Placeholder


# 冒号模式（中英文冒号）
COLON_PATTERN = re.compile(r'[：:]')


def normalize_placeholder_key(key: str) -> str:
    """标准化占位符键名，保留中文字符、数字、字母、下划线和常用符号"""
    # 移除多余空格
    normalized = re.sub(r'\s+', '', key)
    # 保留中文、数字、字母、下划线和常用符号（括号、顿号等）
    normalized = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9_（）()、]', '', normalized)
    return normalized


def extract_underline_placeholders_from_paragraph(paragraph):
    """
    从段落中提取带下划线的填充项

    识别模式：标签文本 + 冒号 + 下划线区域（font.underline=True）
    例如："单   位：________________" 其中下划线部分的run的font.underline=True

    返回：列表，每个元素为 {
        'label': 标签文本,
        'underline_run_indices': 下划线run的索引列表,
        'colon_run_index': 冒号所在run的索引,
        'colon_position': 冒号在该run中的位置
    }
    """
    placeholders = []
    runs = list(paragraph.runs)

    if not runs:
        return placeholders

    # 构建run信息列表，记录每个run的文本、是否下划线、在段落中的字符起始位置
    run_info = []
    char_offset = 0
    for i, run in enumerate(runs):
        text = run.text or ""
        # 检查下划线属性
        is_underline = bool(run.font.underline)
        run_info.append({
            'index': i,
            'text': text,
            'is_underline': is_underline,
            'char_start': char_offset,
            'char_end': char_offset + len(text),
            'run': run
        })
        char_offset += len(text)

    # 找到所有下划线区域（连续的下划线run合并为一个区域）
    underline_regions = []
    i = 0
    while i < len(run_info):
        if run_info[i]['is_underline']:
            region_start = i
            region_end = i
            # 合并连续的下划线run
            while region_end + 1 < len(run_info) and run_info[region_end + 1]['is_underline']:
                region_end += 1
            underline_regions.append({
                'start_index': region_start,
                'end_index': region_end,
                'run_indices': list(range(region_start, region_end + 1))
            })
            i = region_end + 1
        else:
            i += 1

    # 对于每个下划线区域，向前查找标签
    for region in underline_regions:
        label = _find_label_before_underline(run_info, region['start_index'])
        if label:
            placeholders.append({
                'label': label['text'],
                'underline_run_indices': region['run_indices'],
                'colon_run_index': label['colon_run_index'],
                'colon_position': label['colon_position']
            })

    return placeholders


def _find_label_before_underline(run_info, underline_start_index):
    """
    从下划线区域向前查找标签

    规则：
    1. 向前查找最近的冒号（：或:）
    2. 冒号前的文本即为标签（去除前导空格，保留内部空格）
    3. 标签结束于上一个下划线区域或段落开头

    返回：{'text': 标签文本, 'colon_run_index': 冒号所在run索引, 'colon_position': 冒号位置}
    """
    # 收集下划线之前的所有文本和run信息
    text_before = ""
    run_text_mapping = []  # [(run_index, text_start_in_combined, text_end_in_combined)]

    for i in range(underline_start_index):
        text = run_info[i]['text']
        start_pos = len(text_before)
        text_before += text
        run_text_mapping.append((i, start_pos, start_pos + len(text)))

    if not text_before:
        return None

    # 查找最后一个冒号
    colon_match = None
    for match in COLON_PATTERN.finditer(text_before):
        colon_match = match

    if colon_match is None:
        return None

    colon_pos = colon_match.start()

    # 确定冒号所在的run
    colon_run_index = None
    colon_position_in_run = None
    for run_idx, start, end in run_text_mapping:
        if start <= colon_pos < end:
            colon_run_index = run_idx
            colon_position_in_run = colon_pos - start
            break

    if colon_run_index is None:
        return None

    # 提取冒号前的标签文本
    # 标签从上一个下划线区域结束后开始，或从段落开头开始
    label_start = 0
    for i in range(underline_start_index - 1, -1, -1):
        if run_info[i]['is_underline']:
            # 找到前一个下划线区域，标签从其后开始
            label_start = run_info[i]['char_end']
            break

    # 提取标签文本（冒号前的文本）
    label_text = text_before[label_start:colon_pos]

    # 清理标签文本
    # 去除前后空格，但保留内部空格（如"单   位"）
    label_text = label_text.strip()

    if not label_text:
        return None

    return {
        'text': label_text,
        'colon_run_index': colon_run_index,
        'colon_position': colon_position_in_run
    }


def should_process_non_empty_cell(cell_text: str) -> bool:
    """
    判断非空单元格是否需要添加占位符
    条件：包含"："且不包含排除关键词
    """
    if not cell_text or not cell_text.strip():
        return False

    # 必须包含冒号
    if "：" not in cell_text:
        return False

    # 排除关键词列表
    exclude_keywords = ["√", "负责人签字", "盖章"]

    # 如果包含任何排除关键词，则不处理
    for keyword in exclude_keywords:
        if keyword in cell_text:
            return False

    return True


def extract_top_content(cell_text: str) -> tuple[str, int]:
    """
    提取单元格的顶部内容
    通过换行符判断，取第一部分作为顶部内容

    返回: (顶部内容文本, 顶部内容行数)
    """
    if not cell_text or not cell_text.strip():
        return "", 0

    # 按换行符分割
    lines = cell_text.split('\n')

    # 找到连续空行的位置，连续空行前的内容作为顶部
    top_content_lines = []
    consecutive_empty_count = 0

    for line in lines:
        if line.strip() == "":
            consecutive_empty_count += 1
            # 如果连续遇到2个或以上空行，认为顶部内容结束
            if consecutive_empty_count >= 2:
                break
        else:
            # 遇到非空行，重置计数并添加到顶部内容
            consecutive_empty_count = 0
            top_content_lines.append(line)

    # 如果没有找到连续空行，说明整个内容都是顶部内容
    if not top_content_lines:
        top_content_lines = [line for line in lines if line.strip()]

    return '\n'.join(top_content_lines).strip(), len(top_content_lines)


def generate_full_placeholder_key(top_content: str) -> str:
    """
    基于完整的顶部内容生成JSON合法的占位符键名
    处理换行符、引号等特殊字符，确保JSON合法性
    """
    if not top_content or not top_content.strip():
        return ""

    # 1. 移除或替换冒号（通常在内容末尾）
    content = top_content.replace("：", "").replace(":", "")

    # 2. 将双引号替换为单引号
    content = content.replace('"', "'")

    # 3. 将换行符替换为分隔符，保持可读性
    content = content.replace('\n', ' | ')

    # 4. 清理多余的空格
    content = re.sub(r'\s+', ' ', content).strip()

    # 5. 移除其他可能导致JSON问题的字符
    # 保留中文、英文、数字、常用标点符号
    content = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9\s\'\(\)\[\]\{\}，。！？；、|（）【】《》]', '', content)

    # 6. 控制长度，避免键名过长（保留前200个字符）
    if len(content) > 200:
        content = content[:200] + "..."

    # 7. 确保不为空
    if not content.strip():
        return "未命名内容"

    return content.strip()


def is_full_row_cell(row, target_cell) -> bool:
    """判断单元格是否独占整行"""
    target_element = target_cell._element
    return all(cell._element is target_element for cell in row.cells)


def is_independent_cell(row, cell_idx, cell) -> bool:
    """
    判断单元格是否是独立单元格（合并单元格的起始位置）

    独立单元格的定义：
    - 左侧的单元格与当前单元格不是同一个元素（即当前单元格是合并区域的起始）
    - 或者当前单元格是第一列

    Args:
        row: 表格行对象
        cell_idx: 当前单元格索引
        cell: 当前单元格对象

    Returns:
        bool: 是否是独立单元格（合并区域的起始位置）
    """
    cells = row.cells
    current_element = cell._element

    # 检查左侧单元格
    if cell_idx > 0:
        left_cell = cells[cell_idx - 1]
        if left_cell._element is current_element:
            # 左侧单元格与当前单元格是同一个元素，说明不是合并区域的起始
            return False

    # 左侧单元格与当前单元格不同，或者是第一列，说明是合并区域的起始
    return True


def is_merged_cell(cell) -> bool:
    """判断单元格是否为合并单元格"""
    tc = cell._element
    vmerge = tc.xpath(".//w:vMerge")
    return vmerge and vmerge[0].get(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val") != "restart"


def is_start_of_down_merge(cell) -> bool:
    """判断单元格是否为下合并单元格的开始"""
    tc = cell._element
    vmerge = tc.xpath(".//w:vMerge")
    return vmerge and vmerge[0].get(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val") == "restart"


def auto_recognize_placeholders(doc_path: str) -> list[Placeholder]:
    """
    自动识别文档中的待填充位置

    识别规则：
    1. 表格空单元格：右填充、下填充
    2. 表格非空单元格：包含冒号且独占整行
    3. 段落下划线：标签 + 冒号 + 下划线区域

    返回：Placeholder 列表（field_key 自动生成为 $1, $2, $3...）
    """
    doc = Document(doc_path)
    placeholders = []

    # 用于跟踪已处理的合并单元格，避免重复处理
    processed_cells = set()

    # 遍历所有块级元素
    body = doc.element.body
    element_index = 0

    for child in body:
        tag = child.tag.split('}')[-1]

        if tag == 'p':  # 段落
            # 查找对应的段落对象
            para = None
            for p in doc.paragraphs:
                if p._element is child:
                    para = p
                    break

            if para:
                # 识别段落中的下划线填充项
                underline_items = extract_underline_placeholders_from_paragraph(para)

                for item in underline_items:
                    label = item['label']
                    underline_run_indices = item['underline_run_indices']

                    if underline_run_indices:
                        # 生成 path：body[X]/run[Y]
                        first_run_idx = underline_run_indices[0]
                        path = f"body[{element_index}]/run[{first_run_idx}]"

                        placeholders.append(Placeholder(
                            path=path,
                            label=label,
                            field_key=""  # 稍后统一分配 ID
                        ))

            element_index += 1

        elif tag == 'tbl':  # 表格
            # 查找对应的表格对象
            table = None
            for tbl in doc.tables:
                if tbl._element is child:
                    table = tbl
                    break

            if table:
                # 处理表格中的单元格
                table_path = f"body[{element_index}]"

                # 第一阶段：识别空单元格（右填充、下填充）
                rows = table.rows
                num_rows = len(rows)
                num_cols = len(rows[0].cells) if num_rows > 0 else 0

                # 构建表格矩阵
                matrix = []
                for row in rows:
                    matrix.append([cell.text.strip() for cell in row.cells])

                # 右填充
                for r in range(num_rows):
                    for c in range(1, num_cols):
                        if not matrix[r][c].strip():
                            current_cell = rows[r].cells[c]

                            # 如果当前单元格是合并单元格，则跳过
                            if is_merged_cell(current_cell):
                                continue

                            # 当前单元格必须是独立单元格（合并区域的起始）
                            if not is_independent_cell(rows[r], c, current_cell):
                                continue

                            # 向左查找最近的独立单元格
                            source_col = None
                            source_value = None
                            for left_c in range(c - 1, -1, -1):
                                left_cell = rows[r].cells[left_c]

                                # 跳过下合并的起始单元格
                                if is_start_of_down_merge(left_cell):
                                    break

                                # 检查是否是独立单元格且有内容
                                if is_independent_cell(rows[r], left_c, left_cell) and matrix[r][left_c].strip():
                                    source_col = left_c
                                    source_value = matrix[r][left_c]
                                    break

                            # 如果找到了源单元格，进行填充
                            if source_value:
                                path = f"{table_path}/row[{r}]/cell[{c}]"
                                placeholders.append(Placeholder(
                                    path=path,
                                    label=source_value,
                                    field_key=""  # 稍后统一分配 ID
                                ))

                # 收集已经被右填充识别的单元格
                right_filled_cells = set()
                for ph in placeholders:
                    if ph.path.startswith(table_path):
                        # 提取 row 和 cell 索引
                        match = re.search(r'/row\[(\d+)\]/cell\[(\d+)\]$', ph.path)
                        if match:
                            right_filled_cells.add((int(match.group(1)), int(match.group(2))))

                # 下填充（连续传播填充逻辑）
                for c in range(num_cols):
                    seen_keys = {}  # 用于添加序号
                    for r in range(1, num_rows):
                        # 如果已经被右填充识别，跳过
                        if (r, c) in right_filled_cells:
                            continue

                        current_cell = rows[r].cells[c]
                        top_cell = rows[r - 1].cells[c]

                        # 如果当前单元格为空，且不是合并单元格，并且上方单元格有内容
                        if not matrix[r][c].strip() and not is_merged_cell(current_cell) and matrix[r - 1][c].strip():
                            # 检查当前单元格是否是独立单元格（横向合并检查）
                            if not is_independent_cell(rows[r], c, current_cell):
                                continue

                            # 检查上方单元格是否是独立单元格
                            if not is_independent_cell(rows[r - 1], c, top_cell):
                                continue

                            # 检查上方单元格是否已经被右填充识别（说明它是一个待填充位置，不应作为标签）
                            if (r - 1, c) in right_filled_cells:
                                continue

                            # 检查上方单元格左侧是否有内容（如果有，说明上方单元格是"值"位置，不应作为标签）
                            # 只有当上方单元格是第一列，或者左侧为空时，才认为它是"标签"
                            is_label_cell = True
                            if c > 0:
                                # 检查上方单元格左侧是否有内容
                                left_cell = rows[r - 1].cells[c - 1]
                                # 如果左侧单元格与当前单元格是同一个元素（横向合并），则继续向左查找
                                left_col = c - 1
                                while left_col >= 0 and rows[r - 1].cells[left_col]._element is top_cell._element:
                                    left_col -= 1

                                # 如果找到了独立的左侧单元格且有内容，说明上方单元格是"值"位置
                                if left_col >= 0 and matrix[r - 1][left_col].strip():
                                    is_label_cell = False

                            if not is_label_cell:
                                continue

                            top_value = matrix[r - 1][c]

                            path = f"{table_path}/row[{r}]/cell[{c}]"
                            placeholders.append(Placeholder(
                                path=path,
                                label=top_value,
                                field_key=""  # 稍后统一分配 ID
                            ))

                # 第二阶段：识别非空单元格（包含冒号且独占整行）
                for row_idx, row in enumerate(rows):
                    cells = row.cells
                    for cell_idx, cell in enumerate(cells):
                        # 创建单元格的唯一标识
                        cell_id = (element_index, id(cell._element))

                        # 如果这个单元格已经被处理过（可能是合并单元格），跳过
                        if cell_id in processed_cells:
                            continue

                        cell_text = cell.text

                        # 仅处理独占整行的单元格
                        if not is_full_row_cell(row, cell):
                            continue

                        # 判断是否需要处理这个非空单元格
                        if should_process_non_empty_cell(cell_text):
                            # 提取顶部内容和行数
                            top_content, top_lines = extract_top_content(cell_text)

                            if top_content:
                                # 计算插入位置：找到顶部内容对应的最后一个段落
                                # 注意：单元格中的每个段落对应一行文本（通常情况）
                                # 但需要考虑段落可能包含多行文本的情况

                                # 遍历单元格的段落，找到顶部内容结束的段落索引
                                paragraphs = cell.paragraphs
                                insert_after_para_idx = 0

                                # 累计段落文本行数，找到顶部内容结束的位置
                                accumulated_lines = 0
                                for para_idx, para in enumerate(paragraphs):
                                    para_text = para.text
                                    # 计算当前段落的行数（按换行符分割）
                                    para_lines = para_text.count('\n') + 1 if para_text else 1
                                    accumulated_lines += para_lines

                                    if accumulated_lines >= top_lines:
                                        insert_after_para_idx = para_idx
                                        break

                                # 生成 path：在顶部内容的最后一个段落之后插入
                                # 使用 p[N]::after 形式，表示在第 N 个段落之后插入
                                path = f"{table_path}/row[{row_idx}]/cell[{cell_idx}]/p[{insert_after_para_idx}]::after"

                                placeholders.append(Placeholder(
                                    path=path,
                                    label=top_content,
                                    field_key=""  # 稍后统一分配 ID
                                ))

                                # 标记这个单元格已处理
                                processed_cells.add(cell_id)

                                # 如果是合并单元格，标记所有相关单元格为已处理
                                for other_cell_idx, other_cell in enumerate(cells):
                                    if other_cell._element is cell._element and other_cell_idx != cell_idx:
                                        other_cell_id = (element_index, id(other_cell._element))
                                        processed_cells.add(other_cell_id)

                element_index += 1

    # 统一分配 ID：$1, $2, $3...
    for idx, placeholder in enumerate(placeholders, 1):
        placeholder.field_key = f"${idx}"

    return placeholders
