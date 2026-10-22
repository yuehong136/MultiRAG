#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import csv
import io
import json
import logging
import re
from collections import Counter
from io import BytesIO
from typing import Any

import numpy as np
import pandas as pd

# from openpyxl import load_workbook, Workbook
from dateutil.parser import parse as datetime_parse
from xpinyin import Pinyin

from api.db.db_models import db_connection
from api.db.services.knowledgebase_service import KnowledgebaseService
from common import settings
from common.constants import MAXIMUM_TASK_PAGE_NUMBER
from core.nlp import rag_tokenizer, tokenize, tokenize_table
from core.utils.table_metadata import TABLE_METADATA, TABLE_ROW_KEY, table_column_roles
from deepdoc.parser import ExcelParser
from deepdoc.parser.figure_parser import vision_figure_parser_figure_xlsx_wrapper
from deepdoc.parser.utils import get_text


class Excel(ExcelParser):
    def __call__(self, fnm: Any, binary: Any = None, from_page: int = 0, to_page: int = MAXIMUM_TASK_PAGE_NUMBER, callback: Any = None, **kwargs: Any) -> Any:
        if not binary:
            wb = Excel._load_excel_to_workbook(fnm)
        else:
            wb = Excel._load_excel_to_workbook(BytesIO(binary))
        total = 0
        for sheet_name in wb.sheetnames:
            total += Excel._get_actual_row_count(wb[sheet_name])
        res, fails, done = [], [], 0
        rn = 0
        flow_images = []
        pending_cell_images = []
        tables = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            images = Excel._extract_images_from_worksheet(ws, sheetname=sheet_name)
            if images:
                image_descriptions = vision_figure_parser_figure_xlsx_wrapper(images=images, callback=callback, **kwargs)
                if image_descriptions and len(image_descriptions) == len(images):
                    for i, bf in enumerate(image_descriptions):
                        desc = bf[0][1]
                        # VisionFigureParser.__call__ 可能将 descriptions 从列表变为字符串
                        # 需要检查类型以避免 "\n".join(字符串) 导致每个字符被分隔
                        if isinstance(desc, str):
                            images[i]["image_description"] = desc
                        else:
                            images[i]["image_description"] = "\n".join(desc)
                    for img in images:
                        if img["span_type"] == "single_cell" and img.get("image_description"):
                            pending_cell_images.append(img)
                        else:
                            flow_images.append(img)
            try:
                rows = Excel._get_rows_limited(ws)
            except Exception as e:
                logging.warning(f"Skip sheet '{sheet_name}' due to rows access error: {e}")
                continue
            if not rows:
                continue
            headers, header_rows = self._parse_headers(ws, rows)
            if not headers:
                continue
            data = []
            for i, r in enumerate(rows[header_rows:]):
                rn += 1
                if rn - 1 < from_page:
                    continue
                if rn - 1 >= to_page:
                    break
                row_data = self._extract_row_data(ws, r, header_rows + i, len(headers))
                if row_data is None:
                    fails.append(str(i))
                    continue
                if self._is_empty_row(row_data):
                    continue
                data.append(row_data)
                done += 1
            if len(data) == 0:
                continue
            df = pd.DataFrame(data, columns=headers)
            for img in pending_cell_images:
                excel_row = img["row_from"] - 1
                excel_col = img["col_from"] - 1

                df_row_idx = excel_row - header_rows
                if df_row_idx < 0 or df_row_idx >= len(df):
                    flow_images.append(img)
                    continue

                if excel_col < 0 or excel_col >= len(df.columns):
                    flow_images.append(img)
                    continue

                col_name = df.columns[excel_col]

                if not df.iloc[df_row_idx][col_name]:
                    df.iat[df_row_idx, excel_col] = img["image_description"]
            res.append(df)
        for img in flow_images:
            tables.append(
                (
                    (
                        img["image"],  # Image.Image or LazyImage
                        [img["image_description"]],  # description list (must be list)
                    ),
                    [
                        (0, 0, 0, 0, 0)  # dummy position
                    ],
                )
            )
        callback(0.3, (f"Extract records: {from_page + 1}~{min(to_page, from_page + rn)}" + (f"{len(fails)} failure, line: %s..." % (",".join(fails[:3])) if fails else "")))
        return res, tables

    def _parse_headers(self, ws, rows):
        if len(rows) == 0:
            return [], 0
        has_complex_structure = self._has_complex_header_structure(ws, rows)
        if has_complex_structure:
            return self._parse_multi_level_headers(ws, rows)
        else:
            return self._parse_simple_headers(rows)

    def _has_complex_header_structure(self, ws, rows):
        if len(rows) < 1:
            return False
        merged_ranges = list(ws.merged_cells.ranges)
        # 检查前两行是否涉及合并单元格
        for rng in merged_ranges:
            if rng.min_row <= 2:  # 只要合并区域涉及第1或第2行
                return True
        return False

    def _row_looks_like_header(self, row):
        header_like_cells = 0
        data_like_cells = 0
        non_empty_cells = 0
        for cell in row:
            if cell.value is not None:
                non_empty_cells += 1
                val = str(cell.value).strip()
                if self._looks_like_header(val):
                    header_like_cells += 1
                elif self._looks_like_data(val):
                    data_like_cells += 1
        if non_empty_cells == 0:
            return False
        return header_like_cells >= data_like_cells

    def _parse_simple_headers(self, rows):
        if not rows:
            return [], 0
        header_row = rows[0]
        headers = []
        for cell in header_row:
            if cell.value is not None:
                header_value = str(cell.value).strip()
                if header_value:
                    headers.append(header_value)
            else:
                pass
        final_headers = []
        for i, cell in enumerate(header_row):
            if cell.value is not None:
                header_value = str(cell.value).strip()
                if header_value:
                    final_headers.append(header_value)
                else:
                    final_headers.append(f"Column_{i + 1}")
            else:
                final_headers.append(f"Column_{i + 1}")
        return final_headers, 1

    def _parse_multi_level_headers(self, ws, rows):
        if len(rows) < 2:
            return [], 0
        header_rows = self._detect_header_rows(rows)
        if header_rows == 1:
            return self._parse_simple_headers(rows)
        else:
            return self._build_hierarchical_headers(ws, rows, header_rows), header_rows

    def _detect_header_rows(self, rows):
        if len(rows) < 2:
            return 1
        header_rows = 1
        max_check_rows = min(5, len(rows))
        for i in range(1, max_check_rows):
            row = rows[i]
            if self._row_looks_like_header(row):
                header_rows = i + 1
            else:
                break
        return header_rows

    def _looks_like_header(self, value):
        if len(value) < 1:
            return False
        if any(ord(c) > 127 for c in value):
            return True
        if len([c for c in value if c.isalpha()]) >= 2:
            return True
        if any(c in value for c in ["(", ")", "：", ":", "（", "）", "_", "-"]):
            return True
        return False

    def _looks_like_data(self, value):
        if len(value) == 1 and value.upper() in ["Y", "N", "M", "X", "/", "-"]:
            return True
        if value.replace(".", "").replace("-", "").replace(",", "").isdigit():
            return True
        if value.startswith("0x") and len(value) <= 10:
            return True
        return False

    def _build_hierarchical_headers(self, ws, rows, header_rows):
        headers = []
        max_col = max(len(row) for row in rows[:header_rows]) if header_rows > 0 else 0
        merged_ranges = list(ws.merged_cells.ranges)
        for col_idx in range(max_col):
            header_parts = []
            for row_idx in range(header_rows):
                if col_idx < len(rows[row_idx]):
                    cell_value = rows[row_idx][col_idx].value
                    merged_value = self._get_merged_cell_value(ws, row_idx + 1, col_idx + 1, merged_ranges)
                    if merged_value is not None:
                        cell_value = merged_value
                    if cell_value is not None:
                        cell_value = str(cell_value).strip()
                        if cell_value and cell_value not in header_parts and self._is_valid_header_part(cell_value):
                            header_parts.append(cell_value)
            if header_parts:
                header = "-".join(header_parts)
                headers.append(header)
            else:
                headers.append(f"Column_{col_idx + 1}")
        final_headers = [h for h in headers if h and h != "-"]
        return final_headers

    def _is_valid_header_part(self, value):
        if len(value) == 1 and value.upper() in ["Y", "N", "M", "X"]:
            return False
        if value.replace(".", "").replace("-", "").replace(",", "").isdigit():
            return False
        if value in ["/", "-", "+", "*", "="]:
            return False
        return True

    def _get_merged_cell_value(self, ws, row, col, merged_ranges):
        for merged_range in merged_ranges:
            if merged_range.min_row <= row <= merged_range.max_row and merged_range.min_col <= col <= merged_range.max_col:
                return ws.cell(merged_range.min_row, merged_range.min_col).value
        return None

    def _extract_row_data(self, ws, row, absolute_row_idx, expected_cols):
        row_data = []
        merged_ranges = list(ws.merged_cells.ranges)
        actual_row_num = absolute_row_idx + 1
        for col_idx in range(expected_cols):
            cell_value = None
            actual_col_num = col_idx + 1
            try:
                cell_value = ws.cell(row=actual_row_num, column=actual_col_num).value
            except ValueError:
                if col_idx < len(row):
                    cell_value = row[col_idx].value
            if cell_value is None:
                merged_value = self._get_merged_cell_value(ws, actual_row_num, actual_col_num, merged_ranges)
                if merged_value is not None:
                    cell_value = merged_value
                else:
                    cell_value = self._get_inherited_value(ws, actual_row_num, actual_col_num, merged_ranges)
            row_data.append(cell_value)
        return row_data

    def _get_inherited_value(self, ws, row, col, merged_ranges):
        for merged_range in merged_ranges:
            if merged_range.min_row <= row <= merged_range.max_row and merged_range.min_col <= col <= merged_range.max_col:
                return ws.cell(merged_range.min_row, merged_range.min_col).value
        return None

    def _is_empty_row(self, row_data):
        for val in row_data:
            if val is not None and str(val).strip() != "":
                return False
        return True


def trans_datatime(s):
    try:
        return datetime_parse(s.strip()).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def trans_bool(s):
    if re.match(r"(true|yes|是|\*|✓|✔|☑|✅|√)$", str(s).strip(), flags=re.IGNORECASE):
        return "yes"
    if re.match(r"(false|no|否|⍻|×)$", str(s).strip(), flags=re.IGNORECASE):
        return "no"
    return None


def column_data_type(values: Any) -> tuple[list[Any], str]:
    """Infer a common lossless type; mixed or incompatible cells remain text."""
    arr = [None if value is None or pd.isna(value) or not str(value).strip() else value for value in values]
    present = [value for value in arr if value is not None]
    if not present:
        return arr, "text"
    if any(re.match(r"[+-]?0[0-9]+", str(value)) for value in present):
        return [None if value is None else str(value) for value in arr], "text"
    for ty, convert in (("int", int), ("float", float), ("bool", trans_bool), ("datetime", trans_datatime)):
        try:
            converted = [None if value is None else convert(str(value)) for value in arr]
            if any(value is None for value, original in zip(converted, arr, strict=True) if original is not None):
                continue
            if ty == "int" and any(not -(2**63) <= value < 2**63 for value in converted if value is not None):
                return [None if value is None else str(value) for value in arr], "text"
            if ty == "float" and any(not np.isfinite(value) for value in converted if value is not None):
                continue
            return converted, ty
        except (ValueError, TypeError, OverflowError):
            continue
    return [None if value is None else str(value) for value in arr], "text"


def chunk(filename: Any, binary: Any = None, from_page: int = 0, to_page: int = MAXIMUM_TASK_PAGE_NUMBER, lang: str = "Chinese", callback: Any = None, **kwargs: Any) -> Any:
    """
    Excel and csv(txt) format files are supported.
    For csv or txt file, the delimiter between columns is TAB.
    The first line must be column headers.
    Column headers must be meaningful terms inorder to make our NLP model understanding.
    It's good to enumerate some synonyms using slash '/' to separate, and even better to
    enumerate values using brackets like 'gender/sex(male, female)'.
    Here are some examples for headers:
        1. supplier/vendor\tcolor(yellow, red, brown)\tgender/sex(male, female)\tsize(M,L,XL,XXL)
        2. 姓名/名字\t电话/手机/微信\t最高学历（高中，职高，硕士，本科，博士，初中，中技，中专，专科，专升本，MPA，MBA，EMBA）

    Every row in table will be treated as a chunk.
    """
    tbls = []
    is_english = lang.lower() == "english"
    if re.search(r"\.xlsx?$", filename, re.IGNORECASE):
        callback(0.1, "Start to parse.")
        excel_parser = Excel()
        dfs, tbls = excel_parser(filename, binary, from_page=from_page, to_page=to_page, callback=callback, **kwargs)
    elif re.search(r"\.txt$", filename, re.IGNORECASE):
        callback(0.1, "Start to parse.")
        txt = get_text(filename, binary)
        lines = txt.split("\n")
        fails = []
        headers = lines[0].split(kwargs.get("delimiter", "\t"))
        rows = []
        for i, line in enumerate(lines[1:]):
            if i < from_page:
                continue
            if i >= to_page:
                break
            row = list(line.split(kwargs.get("delimiter", "\t")))
            if len(row) != len(headers):
                fails.append(str(i))
                continue
            rows.append(row)

        callback(0.3, (f"Extract records: {from_page}~{min(len(lines), to_page)}" + (f"{len(fails)} failure, line: %s..." % (",".join(fails[:3])) if fails else "")))

        dfs = [pd.DataFrame(np.array(rows), columns=headers)]
    elif re.search(r"\.csv$", filename, re.IGNORECASE):
        callback(0.1, "Start to parse.")
        txt = get_text(filename, binary)
        delimiter = kwargs.get("delimiter", ",")

        reader = csv.reader(io.StringIO(txt), delimiter=delimiter)
        all_rows = list(reader)
        if not all_rows:
            raise ValueError("Empty CSV file")

        headers = all_rows[0]
        fails = []
        rows = []

        for i, row in enumerate(all_rows[1 + from_page : 1 + to_page]):
            if len(row) != len(headers):
                fails.append(str(i + from_page))
                continue
            rows.append(row)

        callback(0.3, (f"Extract records: {from_page}~{from_page + len(rows)}" + (f"{len(fails)} failure, line: {','.join(fails[:3])}..." if fails else "")))

        dfs = [pd.DataFrame(rows, columns=headers)]
    else:
        raise NotImplementedError("file type not supported yet(excel, text, csv supported)")

    config = kwargs.get("parser_config") or {}
    roles = table_column_roles(config)
    use_chunk_data = settings.DOC_ENGINE.lower() in ("infinity", "milvus", "oceanbase")
    fields_map = {"text": "_tks", "int": "_long", "float": "_flt", "datetime": "_dt", "bool": "_kwd"}
    columns: list[str] = []
    values: dict[str, list[Any]] = {}
    for df in dfs:
        for name in ["id", "_id", "index", "idx"]:
            if name in df.columns:
                del df[name]
        df.columns = [str(name) for name in df.columns]
        if len(df.columns) != len(set(df.columns)):
            duplicates = [name for name, count in Counter(df.columns).items() if count > 1]
            raise ValueError(f"Duplicate column names detected: {duplicates}")
        for name in df.columns:
            if name not in values:
                columns.append(name)
                values[name] = []
            values[name].extend(df[name].tolist())
    # Infer once across sheets: the same column cannot get conflicting ES types.
    converted = {name: column_data_type(items) for name, items in values.items()}
    offsets = dict.fromkeys(columns, 0)
    pinyin = Pinyin()
    bases = {name: pinyin.get_pinyins(re.sub(r"(/.*|（[^（）]+?）|\([^()]+?\))", "", name), "_")[0].lower() for name in columns}
    # Keep user columns separate from built-in title_tks/docnm_kwd search fields.
    if not use_chunk_data:
        bases = {name: "table_" + base for name, base in bases.items()}
    if not use_chunk_data and len(set(bases.values())) != len(bases):
        raise ValueError("Table column names resolve to conflicting storage fields.")
    field_map = {(name if use_chunk_data else bases[name] + fields_map[converted[name][1]]): name.replace("_", " ") for name in columns if roles.get(name, "both") in {"metadata", "both"}}
    res = []
    for df in dfs:
        for name in df.columns:
            start = offsets[name]
            df[name] = converted[name][0][start : start + len(df)]
            offsets[name] += len(df)
        for _, row in df.iterrows():
            document = {"docnm_kwd": filename, "title_tks": rag_tokenizer.tokenize(re.sub(r"\.[a-zA-Z]+$", "", filename))}
            text_fields = []
            stored: dict[str, Any] = {}
            for name in df.columns:
                value = row[name]
                if value is None or pd.isna(value) or not str(value).strip():
                    continue
                if hasattr(value, "item"):
                    value = value.item()
                role = roles.get(name, "both")
                if role in {"indexing", "both"}:
                    text_fields.append((name, value))
                if role in {"metadata", "both"}:
                    stored[name] = value
                    if not use_chunk_data:
                        ty = converted[name][1]
                        field = bases[name] + fields_map[ty]
                        document[field] = rag_tokenizer.tokenize(str(value)) if ty == "text" else value
                        if ty == "text":
                            document[bases[name] + "_raw"] = value
            if not text_fields and not stored:
                continue
            if use_chunk_data:
                document["chunk_data"] = stored
            if kwargs.get("table_metadata_carrier", False):
                document[TABLE_METADATA] = stored
                # Metadata-only rows have empty text; include raw cells to avoid collapsing them.
                document[TABLE_ROW_KEY] = json.dumps([text_fields, stored], ensure_ascii=False, sort_keys=True, allow_nan=False)
            tokenize(document, "\n".join(f"- {name}: {value}" for name, value in text_fields), is_english)
            res.append(document)
    if tbls:
        document = {"docnm_kwd": filename, "title_tks": rag_tokenizer.tokenize(re.sub(r"\.[a-zA-Z]+$", "", filename))}
        res.extend(tokenize_table(tbls, document, is_english))
    if kwargs.get("kb_id"):
        with db_connection() as db:
            KnowledgebaseService.update_table_parser_fields(db, kwargs["kb_id"], field_map, columns)
    callback(0.35, "")

    return res


if __name__ == "__main__":
    import sys

    def dummy(prog=None, msg=""):
        pass

    chunk(sys.argv[1], callback=dummy)
