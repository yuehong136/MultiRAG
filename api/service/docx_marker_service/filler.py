# coding=utf-8
"""
根据 path 定位 docx 元素并填充数据
"""

import re
import copy
from pathlib import Path
from docx import Document
from docx.oxml.ns import qn


def parse_insert_path(path: str) -> tuple[str, str | None]:
    """
    解析插入路径，分离基础路径和插入位置
    """
    if '::' in path:
        base_path, position = path.rsplit('::', 1)
        return base_path, position
    return path, None


def parse_path(path: str) -> list[tuple[str, int]]:
    """
    解析 path 字符串为 (类型, 索引) 列表
    """
    pattern = r'(\w+)\[(\d+)\]'
    matches = re.findall(pattern, path)
    return [(name, int(idx)) for name, idx in matches]


def get_element_by_path(doc: Document, path: str):
    """
    根据 path 获取 docx 中对应的元素
    """
    parts = parse_path(path)
    if not parts:
        return None

    # 获取 body 中的元素（段落或表格）
    body = doc.element.body
    
    # 递归查找逻辑，与 parser.py 保持一致
    def find_in_body(target_idx):
        current_idx = 0
        
        def search(container):
            nonlocal current_idx
            for child in container:
                tag = child.tag.split('}')[-1]
                if tag in ('p', 'tbl'):
                    if current_idx == target_idx:
                        return child
                    current_idx += 1
                elif tag == 'sdt':
                    sdt_content = child.find(qn('w:sdtContent'))
                    if sdt_content is not None:
                        res = search(sdt_content)
                        if res is not None:
                            return res
            return None
        
        return search(body)

    # body[n] - 找到第 n 个块级元素
    body_idx = parts[0][1]
    child_element = find_in_body(body_idx)
    
    if child_element is None:
        return None

    # 转换 XML 元素为 python-docx 对象
    current_element = None
    tag = child_element.tag.split('}')[-1]
    if tag == 'p':
        for p in doc.paragraphs:
            if p._element == child_element:
                current_element = p
                break
    elif tag == 'tbl':
        for t in doc.tables:
            if t._element == child_element:
                current_element = t
                break

    if current_element is None:
        return None

    # 只有 body[n]，返回段落或表格
    if len(parts) == 1:
        return current_element

    # 处理后续路径
    for i, (part_type, part_idx) in enumerate(parts[1:], 1):
        if part_type == 'run':
            if hasattr(current_element, 'runs'):
                if part_idx < len(current_element.runs):
                    current_element = current_element.runs[part_idx]
                else:
                    return None
            else:
                return None

        elif part_type == 'row':
            if hasattr(current_element, 'rows'):
                rows = list(current_element.rows)
                if part_idx < len(rows):
                    current_element = rows[part_idx]
                else:
                    return None
            else:
                return None

        elif part_type == 'cell':
            if hasattr(current_element, 'cells'):
                cells = list(current_element.cells)
                if part_idx < len(cells):
                    current_element = cells[part_idx]
                else:
                    return None
            else:
                return None

        elif part_type == 'p':
            if hasattr(current_element, 'paragraphs'):
                if part_idx < len(current_element.paragraphs):
                    current_element = current_element.paragraphs[part_idx]
                else:
                    return None
            else:
                return None

    return current_element


def fill_element(element, value: str):
    from docx.table import _Cell
    from docx.text.paragraph import Paragraph
    from docx.text.run import Run

    if isinstance(element, Run):
        element.text = value
    elif isinstance(element, Paragraph):
        if element.runs:
            first_run = element.runs[0]
            for run in element.runs[1:]:
                run._element.getparent().remove(run._element)
            first_run.text = value
        else:
            element.add_run(value)
    elif isinstance(element, _Cell):
        if element.paragraphs:
            para = element.paragraphs[0]
            if para.runs:
                para.runs[0].text = value
                for run in para.runs[1:]:
                    run._element.getparent().remove(run._element)
            else:
                para.add_run(value)
            for p in element.paragraphs[1:]:
                p._element.getparent().remove(p._element)


def insert_run_at(paragraph, ref_run_index: int, position: str, text: str):
    new_run = paragraph.add_run(text)
    run_element = new_run._element
    runs = paragraph.runs
    if position == 'before':
        if ref_run_index < len(runs) - 1:
            ref_element = runs[ref_run_index]._element
            ref_element.addprevious(run_element)
    elif position == 'after':
        if ref_run_index < len(runs) - 1:
            ref_element = runs[ref_run_index]._element
            ref_element.addnext(run_element)


def insert_paragraph_at(cell, ref_para_index: int, position: str, text: str):
    new_para = cell.add_paragraph(text)
    para_element = new_para._element
    paragraphs = cell.paragraphs
    num_paras = len(paragraphs)
    if position == 'prepend':
        if num_paras > 1:
            first_element = paragraphs[0]._element
            first_element.addprevious(para_element)
    elif position == 'before':
        if ref_para_index < num_paras - 1:
            ref_element = paragraphs[ref_para_index]._element
            ref_element.addprevious(para_element)
    elif position == 'after':
        if ref_para_index < num_paras - 1:
            ref_element = paragraphs[ref_para_index]._element
            ref_element.addnext(para_element)


def get_parent_element_by_path(doc: Document, path: str):
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
    from docx.table import _Cell
    from docx.text.paragraph import Paragraph
    doc = Document(doc_path)
    sorted_placeholders = sorted(placeholders, key=lambda p: p['path'], reverse=True)

    for placeholder in sorted_placeholders:
        placeholder_id = placeholder.get('field_key') or placeholder.get('id')
        if not placeholder_id or placeholder_id not in data:
            continue
        value = data[placeholder_id]
        full_path = placeholder['path']
        base_path, insert_position = parse_insert_path(full_path)

        if insert_position is None:
            element = get_element_by_path(doc, base_path)
            if element:
                fill_element(element, str(value))
        else:
            if insert_position in ('prepend', 'append'):
                container = get_element_by_path(doc, base_path)
                if container and isinstance(container, _Cell):
                    insert_paragraph_at(container, 0, insert_position, str(value))
            else:
                parent, elem_type, elem_index = get_parent_element_by_path(doc, base_path)
                if parent is None: continue
                if elem_type == 'run' and isinstance(parent, Paragraph):
                    insert_run_at(parent, elem_index, insert_position, str(value))
                elif elem_type == 'p' and isinstance(parent, _Cell):
                    insert_paragraph_at(parent, elem_index, insert_position, str(value))

    doc.save(output_path)
    return output_path