from docx.table import Table, _Cell
from typing import Dict, List, Tuple
import html


class TableConverter:
    @staticmethod
    def convert_to_html(table: Table) -> str:
        html_builder = []
        html_builder.append('<table border="1">\n')

        # 获取合并单元格信息
        span_info = TableConverter._get_span_info(table)

        # 检查是否有表头
        has_header = TableConverter._has_header_row(table)

        if has_header:
            html_builder.append('  <thead>\n')

        # 遍历行
        for row_idx, row in enumerate(table.rows):
            # 如果是第二行且第一行是表头，添加tbody标签
            if row_idx == 1 and has_header:
                html_builder.append('  </thead>\n  <tbody>\n')
            elif row_idx == 0 and not has_header:
                html_builder.append('  <tbody>\n')

            html_builder.append('    <tr>\n')

            # 遍历单元格
            for col_idx, cell in enumerate(row.cells):
                # 跳过被合并的从属单元格
                if TableConverter._is_subordinate_cell(cell):
                    continue

                # 获取单元格的合并信息
                colspan, rowspan = span_info.get((row_idx, col_idx), (1, 1))

                # 判断是否是表头单元格
                tag = 'th' if TableConverter._is_header_cell(cell) else 'td'

                # 构建单元格标签
                html_builder.append(f'      <{tag}')
                if colspan > 1:
                    html_builder.append(f' colspan="{colspan}"')
                if rowspan > 1:
                    html_builder.append(f' rowspan="{rowspan}"')

                html_builder.append('>')
                html_builder.append(TableConverter._get_cell_content(cell))
                html_builder.append(f'</{tag}>\n')

            html_builder.append('    </tr>\n')

        # 添加闭合标签
        if has_header:
            html_builder.append('  </tbody>\n')
        html_builder.append('</table>')

        return ''.join(html_builder)

    @staticmethod
    def _get_span_info(table: Table) -> Dict[Tuple[int, int], Tuple[int, int]]:
        """获取所有单元格的合并信息"""
        span_info = {}

        for row_idx, row in enumerate(table.rows):
            for col_idx, cell in enumerate(row.cells):
                # 获取水平合并信息
                colspan = TableConverter._get_colspan(cell)
                # 获取垂直合并信息
                rowspan = TableConverter._get_rowspan(cell, row_idx, table)

                if colspan > 1 or rowspan > 1:
                    span_info[(row_idx, col_idx)] = (colspan, rowspan)

        return span_info

    @staticmethod
    def _get_colspan(cell: _Cell) -> int:
        """获取单元格的水平合并数"""
        try:
            grid_span = cell._tc.tcPr.gridSpan_val
            return int(grid_span) if grid_span else 1
        except AttributeError:
            return 1

    @staticmethod
    def _get_rowspan(cell: _Cell, row_idx: int, table: Table) -> int:
        """获取单元格的垂直合并数"""
        try:
            vmerge = cell._tc.tcPr.vMerge_val
            if not vmerge or vmerge != "restart":
                return 1

            # 计算向下合并的行数
            rowspan = 1
            for i in range(row_idx + 1, len(table.rows)):
                next_cell = table.rows[i].cells[TableConverter._get_cell_index(cell)]
                if not TableConverter._is_continued_merge(next_cell):
                    break
                rowspan += 1
            return rowspan
        except AttributeError:
            return 1

    @staticmethod
    def _is_continued_merge(cell: _Cell) -> bool:
        """判断单元格是否是垂直合并的延续"""
        try:
            vmerge = cell._tc.tcPr.vMerge_val
            return vmerge is not None and vmerge == "continue"
        except AttributeError:
            return False

    @staticmethod
    def _is_subordinate_cell(cell: _Cell) -> bool:
        """判断是否是被合并的从属单元格"""
        try:
            vmerge = cell._tc.tcPr.vMerge_val
            return vmerge is not None and vmerge == "continue"
        except AttributeError:
            return False

    @staticmethod
    def _get_cell_index(cell: _Cell) -> int:
        """获取单元格在行中的索引"""
        for i, c in enumerate(cell._tc.p_p):
            if c == cell._tc:
                return i
        return -1

    @staticmethod
    def _is_header_cell(cell: _Cell) -> bool:
        """判断是否是表头单元格"""
        try:
            # 可以根据实际需求调整判断逻辑
            return cell._tc.tcPr.shd_val is not None
        except AttributeError:
            return False

    @staticmethod
    def _has_header_row(table: Table) -> bool:
        """判断表格是否有表头行"""
        if not table.rows:
            return False
        return any(TableConverter._is_header_cell(cell) for cell in table.rows[0].cells)

    @staticmethod
    def _get_cell_content(cell: _Cell) -> str:
        """获取单元格内容"""
        content = []
        for paragraph in cell.paragraphs:
            if content:
                content.append('<br/>')
            content.append(html.escape(paragraph.text))
        return ''.join(content)
