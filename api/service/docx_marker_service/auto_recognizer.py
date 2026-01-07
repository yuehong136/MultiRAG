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
from docx.oxml.ns import qn

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
    """
    placeholders = []
    runs = list(paragraph.runs)

    if not runs:
        return placeholders

    # 构建run信息列表
    run_info = []
    char_offset = 0
    for i, run in enumerate(runs):
        text = run.text or ""
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

    # 找到所有下划线区域
    underline_regions = []
    i = 0
    while i < len(run_info):
        if run_info[i]['is_underline']:
            region_start = i
            region_end = i
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
    text_before = ""
    run_text_mapping = []

    for i in range(underline_start_index):
        text = run_info[i]['text']
        start_pos = len(text_before)
        text_before += text
        run_text_mapping.append((i, start_pos, start_pos + len(text)))

    if not text_before:
        return None

    colon_match = None
    for match in COLON_PATTERN.finditer(text_before):
        colon_match = match

    if colon_match is None:
        return None

    colon_pos = colon_match.start()
    colon_run_index = None
    for run_idx, start, end in run_text_mapping:
        if start <= colon_pos < end:
            colon_run_index = run_idx
            break

    if colon_run_index is None:
        return None

    label_start = 0
    for i in range(underline_start_index - 1, -1, -1):
        if run_info[i]['is_underline']:
            label_start = run_info[i]['char_end']
            break

    label_text = text_before[label_start:colon_pos].strip()
    if not label_text:
        return None

    return {
        'text': label_text,
        'colon_run_index': colon_run_index,
        'colon_position': colon_pos - run_text_mapping[colon_run_index][1]
    }


def should_process_non_empty_cell(cell_text: str) -> bool:
    if not cell_text or not cell_text.strip():
        return False
    if "：" not in cell_text:
        return False
    exclude_keywords = ["√", "负责人签字", "盖章"]
    for keyword in exclude_keywords:
        if keyword in cell_text:
            return False
    return True


def extract_top_content(cell_text: str) -> tuple[str, int]:
    if not cell_text or not cell_text.strip():
        return "", 0
    lines = cell_text.split('\n')
    top_content_lines = []
    consecutive_empty_count = 0
    for line in lines:
        if line.strip() == "":
            consecutive_empty_count += 1
            if consecutive_empty_count >= 2:
                break
        else:
            consecutive_empty_count = 0
            top_content_lines.append(line)
    if not top_content_lines:
        top_content_lines = [line for line in lines if line.strip()]
    return '\n'.join(top_content_lines).strip(), len(top_content_lines)


def is_full_row_cell(row, target_cell) -> bool:
    target_element = target_cell._element
    return all(cell._element is target_element for cell in row.cells)

def is_independent_cell(row, cell_idx, cell) -> bool:
    cells = row.cells
    current_element = cell._element
    if cell_idx > 0:
        left_cell = cells[cell_idx - 1]
        if left_cell._element is current_element:
            return False
    return True

def is_merged_cell(cell) -> bool:
    tc = cell._element
    vmerge = tc.xpath(".//w:vMerge")
    if not vmerge:
        return False
    return vmerge[0].get(qn("w:val")) != "restart"

def is_start_of_down_merge(cell) -> bool:
    tc = cell._element
    vmerge = tc.xpath(".//w:vMerge")
    if not vmerge:
        return False
    return vmerge[0].get(qn("w:val")) == "restart"

def auto_recognize_placeholders(doc_path: str) -> list[Placeholder]:
    doc = Document(doc_path)
    placeholders = []
    processed_cells = set()
    
    body = doc.element.body
    element_index = 0

    def process_element(child, index):
        tag = child.tag.split('}')[-1]

        if tag == 'p':
            para = None
            for p in doc.paragraphs:
                if p._element == child:
                    para = p
                    break

            if para:
                underline_items = extract_underline_placeholders_from_paragraph(para)
                for item in underline_items:
                    label = item['label']
                    underline_run_indices = item['underline_run_indices']
                    if underline_run_indices:
                        first_run_idx = underline_run_indices[0]
                        path = f"body[{index}]/run[{first_run_idx}]"
                        placeholders.append(Placeholder(
                            path=path,
                            label=label,
                            field_key=""
                        ))
            return True

        elif tag == 'tbl':
            table = None
            for tbl in doc.tables:
                if tbl._element == child:
                    table = tbl
                    break

            if table:
                table_path = f"body[{index}]"
                rows = table.rows
                num_rows = len(rows)
                num_cols = len(rows[0].cells) if num_rows > 0 else 0
                matrix = [[cell.text.strip() for cell in row.cells] for row in rows]

                # 右填充
                for r in range(num_rows):
                    for c in range(1, num_cols):
                        if not matrix[r][c].strip():
                            current_cell = rows[r].cells[c]
                            if is_merged_cell(current_cell): continue
                            if not is_independent_cell(rows[r], c, current_cell): continue
                            
                            source_value = None
                            for left_c in range(c - 1, -1, -1):
                                left_cell = rows[r].cells[left_c]
                                if is_start_of_down_merge(left_cell): break
                                if is_independent_cell(rows[r], left_c, left_cell) and matrix[r][left_c].strip():
                                    source_value = matrix[r][left_c]
                                    break
                            
                            if source_value:
                                path = f"{table_path}/row[{r}]/cell[{c}]"
                                placeholders.append(Placeholder(path=path, label=source_value, field_key=""))

                # 收集已经被右填充识别的单元格
                right_filled_cells = set()
                for ph in placeholders:
                    if ph.path.startswith(table_path):
                        match = re.search(r'/row[[](\d+)]/cell[[](\d+)]$', ph.path)
                        if match:
                            right_filled_cells.add((int(match.group(1)), int(match.group(2))))

                # 下填充
                for c in range(num_cols):
                    for r in range(1, num_rows):
                        if (r, c) in right_filled_cells: continue
                        current_cell = rows[r].cells[c]
                        top_cell = rows[r - 1].cells[c]

                        if not matrix[r][c].strip() and not is_merged_cell(current_cell) and matrix[r - 1][c].strip():
                            if not is_independent_cell(rows[r], c, current_cell): continue
                            if not is_independent_cell(rows[r - 1], c, top_cell): continue
                            if (r - 1, c) in right_filled_cells: continue

                            is_label_cell = True
                            if c > 0:
                                left_col = c - 1
                                while left_col >= 0 and rows[r - 1].cells[left_col]._element is top_cell._element:
                                    left_col -= 1
                                if left_col >= 0 and matrix[r - 1][left_col].strip():
                                    is_label_cell = False

                            if is_label_cell:
                                path = f"{table_path}/row[{r}]/cell[{c}]"
                                placeholders.append(Placeholder(path=path, label=matrix[r - 1][c], field_key=""))

                # 非空单元格
                for row_idx, row in enumerate(rows):
                    for cell_idx, cell in enumerate(row.cells):
                        cell_id = (index, id(cell._element))
                        if cell_id in processed_cells: continue
                        if not is_full_row_cell(row, cell): continue

                        if should_process_non_empty_cell(cell.text):
                            top_content, top_lines = extract_top_content(cell.text)
                            if top_content:
                                accumulated_lines = 0
                                insert_after_para_idx = 0
                                for para_idx, para in enumerate(cell.paragraphs):
                                    para_lines = para.text.count('\n') + 1 if para.text else 1
                                    accumulated_lines += para_lines
                                    if accumulated_lines >= top_lines:
                                        insert_after_para_idx = para_idx
                                        break
                                
                                path = f"{table_path}/row[{row_idx}]/cell[{cell_idx}]/p[{insert_after_para_idx}]::after"
                                placeholders.append(Placeholder(path=path, label=top_content, field_key=""))
                                processed_cells.add(cell_id)
                                for o_idx, o_cell in enumerate(row.cells):
                                    if o_cell._element is cell._element:
                                        processed_cells.add((index, id(o_cell._element)))
            return True

        elif tag == 'sdt':
            sdt_content = child.find(qn('w:sdtContent'))
            if sdt_content is not None:
                nonlocal element_index
                for sdt_child in sdt_content:
                    if process_element(sdt_child, element_index):
                        element_index += 1
                return False
        return False

    for child in body:
        if process_element(child, element_index):
            element_index += 1

    for idx, placeholder in enumerate(placeholders, 1):
        placeholder.field_key = f"${idx}"

    return placeholders