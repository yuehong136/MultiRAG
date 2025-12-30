# coding=utf-8
"""
根据 path 定位 docx 元素并填充数据

支持两种模式:
- 替换模式: path 不包含 "::"，直接替换元素内容
- 插入模式: path 包含 "::before"/"::after" 等，在指定位置插入新内容
"""

import re
from pathlib import Path
from typing import TYPE_CHECKING

from docx import Document

if TYPE_CHECKING:
    from docx.document import Document as DocumentType


def parse_insert_path(path: str) -> tuple[str, str | None]:
    """
    解析插入路径，分离基础路径和插入位置

    例如:
    "body[0]/p[0]/run[1]::before" -> ("body[0]/p[0]/run[1]", "before")
    "body[0]/p[0]/run[1]::after" -> ("body[0]/p[0]/run[1]", "after")
    "body[0]/row[0]/cell[0]::prepend" -> ("body[0]/row[0]/cell[0]", "prepend")
    "body[0]/row[0]/cell[0]::append" -> ("body[0]/row[0]/cell[0]", "append")
    "body[0]/p[0]" -> ("body[0]/p[0]", None)
    """
    if '::' in path:
        base_path, position = path.rsplit('::', 1)
        return base_path, position
    return path, None


def parse_path(path: str) -> list[tuple[str, int]]:
    """
    解析 path 字符串为 (类型, 索引) 列表

    例如:
    "body[1]/row[0]/cell[2]" -> [("body", 1), ("row", 0), ("cell", 2)]
    "body[0]/run[1]" -> [("body", 0), ("run", 1)]
    """
    pattern = r'(\w+)\[(\d+)\]'
    matches = re.findall(pattern, path)
    return [(name, int(idx)) for name, idx in matches]


def get_element_by_path(doc: "DocumentType", path: str):
    """
    根据 path 获取 docx 中对应的元素

    支持的 path 格式:
    - body[n] -> 段落或表格
    - body[n]/run[m] -> 段落中的 run
    - body[n]/row[r]/cell[c] -> 表格单元格
    - body[n]/row[r]/cell[c]/p[p] -> 单元格中的段落
    - body[n]/row[r]/cell[c]/p[p]/run[m] -> 单元格段落中的 run
    """
    parts = parse_path(path)

    if not parts:
        return None

    # 获取 body 中的元素（段落或表格）
    body = doc.element.body
    element_index = 0
    current_element = None

    # body[n] - 找到第 n 个块级元素
    body_idx = parts[0][1]
    block_count = 0

    for child in body:
        tag = child.tag.split('}')[-1]
        if tag in ('p', 'tbl'):
            if block_count == body_idx:
                if tag == 'p':
                    # 找到对应的 Paragraph 对象
                    for p in doc.paragraphs:
                        if p._element is child:
                            current_element = p
                            break
                else:
                    # 找到对应的 Table 对象
                    for t in doc.tables:
                        if t._element is child:
                            current_element = t
                            break
                break
            block_count += 1

    if current_element is None:
        return None

    # 只有 body[n]，返回段落或表格
    if len(parts) == 1:
        return current_element

    # 处理后续路径
    for i, (part_type, part_idx) in enumerate(parts[1:], 1):
        if part_type == 'run':
            # 段落中的 run
            if hasattr(current_element, 'runs'):
                if part_idx < len(current_element.runs):
                    current_element = current_element.runs[part_idx]
                else:
                    return None
            else:
                return None

        elif part_type == 'row':
            # 表格中的行
            if hasattr(current_element, 'rows'):
                # 缓存 rows 避免重复创建
                rows = list(current_element.rows)
                if part_idx < len(rows):
                    current_element = rows[part_idx]
                else:
                    return None
            else:
                return None

        elif part_type == 'cell':
            # 行中的单元格
            if hasattr(current_element, 'cells'):
                cells = list(current_element.cells)
                if part_idx < len(cells):
                    current_element = cells[part_idx]
                else:
                    return None
            else:
                return None

        elif part_type == 'p':
            # 单元格中的段落
            if hasattr(current_element, 'paragraphs'):
                if part_idx < len(current_element.paragraphs):
                    current_element = current_element.paragraphs[part_idx]
                else:
                    return None
            else:
                return None

    return current_element


def fill_element(element, value: str):
    """
    填充元素的内容（替换模式）

    - 如果是 run，直接设置 text
    - 如果是段落，清空后添加 run
    - 如果是单元格，清空后设置第一个段落的文本
    """
    from docx.table import _Cell
    from docx.text.paragraph import Paragraph
    from docx.text.run import Run

    if isinstance(element, Run):
        element.text = value

    elif isinstance(element, Paragraph):
        # 保留第一个 run 的样式，清空其他
        if element.runs:
            first_run = element.runs[0]
            # 清空所有 run
            for run in element.runs[1:]:
                run._element.getparent().remove(run._element)
            first_run.text = value
        else:
            element.add_run(value)

    elif isinstance(element, _Cell):
        # 单元格：设置第一个段落的文本
        if element.paragraphs:
            para = element.paragraphs[0]
            if para.runs:
                para.runs[0].text = value
                # 清空其他 run
                for run in para.runs[1:]:
                    run._element.getparent().remove(run._element)
            else:
                para.add_run(value)
            # 清空其他段落
            for p in element.paragraphs[1:]:
                p._element.getparent().remove(p._element)


def insert_run_at(paragraph, ref_run_index: int, position: str, text: str):
    """
    在段落中指定位置插入新 Run

    Args:
        paragraph: 段落对象
        ref_run_index: 参考 Run 的索引
        position: "before" 或 "after"
        text: 要插入的文本
    """
    new_run = paragraph.add_run(text)
    run_element = new_run._element

    runs = paragraph.runs
    if position == 'before':
        if ref_run_index < len(runs) - 1:  # -1 因为新 run 已经添加到末尾
            ref_element = runs[ref_run_index]._element
            ref_element.addprevious(run_element)
    elif position == 'after':
        if ref_run_index < len(runs) - 1:
            ref_element = runs[ref_run_index]._element
            ref_element.addnext(run_element)
        # 如果是最后一个 run 的 after，新 run 已经在末尾，无需移动


def insert_paragraph_at(cell, ref_para_index: int, position: str, text: str):
    """
    在单元格中指定位置插入新段落

    Args:
        cell: 单元格对象
        ref_para_index: 参考段落的索引
        position: "before", "after", "prepend", "append"
        text: 要插入的文本
    """
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    # 创建新段落
    new_para = cell.add_paragraph(text)
    para_element = new_para._element

    paragraphs = cell.paragraphs
    num_paras = len(paragraphs)

    if position == 'prepend':
        # 移到最前面
        if num_paras > 1:
            first_element = paragraphs[0]._element
            first_element.addprevious(para_element)
    elif position == 'append':
        # 已经在末尾，无需移动
        pass
    elif position == 'before':
        if ref_para_index < num_paras - 1:  # -1 因为新段落已经添加到末尾
            ref_element = paragraphs[ref_para_index]._element
            ref_element.addprevious(para_element)
    elif position == 'after':
        if ref_para_index < num_paras - 1:
            ref_element = paragraphs[ref_para_index]._element
            ref_element.addnext(para_element)
        # 如果是最后一个段落的 after，新段落已经在末尾，无需移动


def get_parent_element_by_path(doc: "DocumentType", path: str):
    """
    获取 path 指向元素的父元素，用于插入操作

    返回: (parent_element, element_type, element_index)
    - 对于 run: 返回 (paragraph, "run", run_index)
    - 对于 p (在单元格内): 返回 (cell, "p", para_index)
    """
    parts = parse_path(path)
    if len(parts) < 2:
        return None, None, None

    last_part = parts[-1]
    parent_path = '/'.join(f"{name}[{idx}]" for name, idx in parts[:-1])

    parent_element = get_element_by_path(doc, parent_path)
    if parent_element is None:
        return None, None, None

    return parent_element, last_part[0], last_part[1]


def fill_document(doc_path: str, placeholders: list[dict], data: dict, output_path: str) -> str:
    """
    填充文档

    Args:
        doc_path: 原始文档路径
        placeholders: 待填项列表 [{"path": "...", "label": "...", "id": "..."}]
        data: 填充数据 {"id": "value", ...}
        output_path: 输出文档路径

    Returns:
        输出文档路径

    支持两种模式:
    - 替换模式: path 不包含 "::"，直接替换元素内容
    - 插入模式: path 包含 "::before"/"::after" 等，在指定位置插入新内容
    """
    from docx.table import _Cell
    from docx.text.paragraph import Paragraph

    doc = Document(doc_path)

    # 按 path 排序，从后往前处理，避免插入后索引错位
    # 插入操作会改变后续元素的索引，所以需要倒序处理
    sorted_placeholders = sorted(
        placeholders,
        key=lambda p: p['path'],
        reverse=True
    )

    for placeholder in sorted_placeholders:
        # 兼容两种字段名：新格式使用 'field_key'，旧格式可能使用 'id'
        placeholder_id = placeholder.get('field_key') or placeholder.get('id')
        if not placeholder_id or placeholder_id not in data:
            continue

        value = data[placeholder_id]
        full_path = placeholder['path']

        # 解析插入路径
        base_path, insert_position = parse_insert_path(full_path)

        if insert_position is None:
            # 替换模式：直接替换元素内容
            element = get_element_by_path(doc, base_path)
            if element:
                fill_element(element, str(value))
        else:
            # 插入模式：在指定位置插入新内容
            if insert_position in ('prepend', 'append'):
                # prepend/append 是针对容器（单元格）的操作
                container = get_element_by_path(doc, base_path)
                if container and isinstance(container, _Cell):
                    insert_paragraph_at(container, 0, insert_position, str(value))
            else:
                # before/after 是针对具体元素的操作
                parent, elem_type, elem_index = get_parent_element_by_path(doc, base_path)
                if parent is None:
                    continue

                if elem_type == 'run' and isinstance(parent, Paragraph):
                    # 在段落中插入 Run
                    insert_run_at(parent, elem_index, insert_position, str(value))
                elif elem_type == 'p' and isinstance(parent, _Cell):
                    # 在单元格中插入段落
                    insert_paragraph_at(parent, elem_index, insert_position, str(value))

    doc.save(output_path)
    return output_path
