import pytest
from bs4 import BeautifulSoup

from api.service.docx2zjform_service.analyzer.table_analyzer_util import is_multiple_tables_with_name, \
    is_single_normal_table, is_single_empty_table_with_multiple_br, is_one_column_multiple_rows_table

# 测试数据
INPUTS_TABLE_HTML = """
<table border="1">
  <tbody>
    <tr>
      <td>工号</td>
      <td></td>
      <td>姓名</td>
      <td></td>
      <td>性别</td>
      <td></td>
      <td>出生年月</td>
      <td></td>
    </tr>
    <tr>
      <td>岗位类别</td>
      <td></td>
      <td>岗位系列</td>
      <td></td>
      <td>岗位级别</td>
      <td>岗位级别</td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td>职务</td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
</table>
"""

SINGLE_NORMAL_TABLE_HTML = """
<table border="1">
    <tbody>
        <tr>
            <td>学习起始时间</td>
            <td>学习截止时间</td>
            <td>毕业学校</td>
            <td>所学专业</td>
            <td>学习证明人</td>
        </tr>
        <tr>
            <td></td>
            <td></td>
            <td></td>
            <td></td>
            <td></td>
        </tr>
        <tr>
            <td></td>
            <td></td>
            <td></td>
            <td></td>
            <td></td>
        </tr>
    </tbody>
    </table>
"""

MULTIPLE_TABLES_WITH_NAME_HTML = """
<table border="1">
  <tbody>
    <tr>
      <td>科研论文</td>
      <td>论文类型</td>
      <td>论文名称</td>
      <td>发布时间</td>
      <td>发表单位</td>
      <td>所属研究学科</td>
    </tr>
    <tr>
      <td>科研论文</td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td>科研论文</td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td>科研论文</td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td>科研论文</td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td>科研论文</td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td>科研专利</td>
      <td>专利类型</td>
      <td>专利名称</td>
      <td>专利编号</td>
      <td>所属单位</td>
      <td>专利授权日期</td>
    </tr>
    <tr>
      <td>科研专利</td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td>科研专利</td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td>科研专利</td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td>科研专利</td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td>科研专利</td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td>科研获奖成果</td>
      <td>科研获奖成果</td>
      <td>科研获奖成果</td>
      <td>科研获奖成果</td>
      <td>科研获奖成果</td>
      <td>科研获奖成果</td>
    </tr>
    <tr>
      <td>获奖级别</td>
      <td>获奖级别</td>
      <td>获奖成果名称</td>
      <td>获奖日期</td>
      <td>奖励名称</td>
      <td>获奖单位</td>
    </tr>
    <tr>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
    <tr>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
    </tr>
</table>
"""

SINGLE_EMPTY_TABLE_WITH_MULTIPLE_BR_HTML = """
<table border="1">
  <tbody>
    <tr>
      <td><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/></td>
    </tr>
</table>
"""

ONE_COLUMN_MULTIPLE_ROWS_TABLE_HTML = """
<table border="1">
  <tbody>
    <tr>
      <td>民主测评情况及基层组织考核意见：<br/>□是/ □否有涉及《XX大学教师职业行为负面清单》的行为。<br/><br/><br/><br/><br/>                                  负责人签字：        年   月   日</td>
    </tr>
    <tr>
      <td>单位考核工作小组意见：<br/>    □是/ □否有涉及《XX大学教师职业行为负面清单》的行为。<br/><br/><br/><br/>                                 负责人签字：           单位盖章<br/>                                                    年    月   日</td>
    </tr>
    <tr>
      <td>校考核工作领导小组意见：<br/><br/><br/><br/>                                         （盖章）        年   月  日</td>
    </tr>
</table>
"""


class TestTableDetection:
    """测试表格检测相关函数"""

    # @pytest.mark.parametrize("html_content,expected", [
    #     (SINGLE_TABLE_HTML, True),
    #     (MULTIPLE_TABLES_HTML, False),
    #     (EMPTY_TABLE_HTML, False),
    #     (INVALID_HTML, False),
    # ])
    # def test_is_single_table_parametrized(self, html_content, expected):
    #     """使用参数化测试is_single_table函数的各种情况"""
    #     result = is_single_table(html_content)
    #     assert result == expected

    def test_is_multiple_tables_with_name(self):
        result = is_multiple_tables_with_name(MULTIPLE_TABLES_WITH_NAME_HTML)
        assert result is True

    def test_is_single_normal_table(self):
        result = is_single_normal_table(SINGLE_NORMAL_TABLE_HTML)
        assert result is True

    def test_is_inputs_table(self):
        result = is_single_normal_table(INPUTS_TABLE_HTML)
        assert result is False

    def test_is_single_empty_table_with_multiple_br(self):
        result = is_single_empty_table_with_multiple_br(SINGLE_EMPTY_TABLE_WITH_MULTIPLE_BR_HTML)
        assert result is True

    def test_is_one_column_multiple_rows_table(self):
        result = is_one_column_multiple_rows_table(ONE_COLUMN_MULTIPLE_ROWS_TABLE_HTML)
        assert result is True


if __name__ == '__main__':
    pytest.main(['-v'])
