import re


def extract_js_function(text, function_name=None):
    """
    从文本中提取JavaScript函数并去除注释

    Args:
        text (str): 包含JavaScript函数的文本
        function_name (str, optional): 指定要提取的函数名，如果为None则提取第一个找到的函数

    Returns:
        str: 提取到的JavaScript函数(不含注释)，如果没有找到则返回空字符串
    """

    def find_matching_brace(text, start):
        """
        找到匹配的右大括号位置
        """
        count = 1
        i = start
        while i < len(text) and count > 0:
            if text[i] == '{':
                count += 1
            elif text[i] == '}':
                count -= 1
            i += 1
        return i if count == 0 else -1

    def extract_complete_function(text, start):
        """
        从起始位置提取完整的函数定义
        """
        body_start = text.find('{', start)
        if body_start == -1:
            return ""

        body_end = find_matching_brace(text, body_start + 1)
        if body_end == -1:
            return ""

        # 提取完整函数文本，包括函数声明和函数体
        full_function = text[start:body_end].strip()

        # 如果提取的内容包含在代码块中，需要去除代码块标记
        if '```' in full_function:
            full_function = re.sub(r'```javascript\s*', '', full_function)
            full_function = re.sub(r'\s*```', '', full_function)

        return full_function

    def remove_comments(js_code):
        """
        去除JavaScript代码中的注释

        Args:
            js_code (str): JavaScript代码

        Returns:
            str: 去除注释后的代码
        """
        # 去除单行注释
        js_code = re.sub(r'//.*?(?=\n|$)', '', js_code)

        # 去除多行注释
        js_code = re.sub(r'/\*.*?\*/', '', js_code, flags=re.DOTALL)

        # 清理可能产生的多余空行
        js_code = re.sub(r'\n\s*\n+', '\n', js_code)

        return js_code.strip()

    # 构建函数声明的正则表达式
    if function_name:
        function_pattern = rf"function\s+{function_name}\s*\([^)]*\)"
    else:
        function_pattern = r"function\s+[\w\$]+\s*\([^)]*\)"

    # 在代码块中查找
    js_block_match = re.search(rf"```javascript\s*({function_pattern})", text)
    if js_block_match:
        start_pos = text.find(js_block_match.group(1))
        if start_pos != -1:
            extracted_function = extract_complete_function(text, start_pos)
            return remove_comments(extracted_function)

    # 直接在文本中查找
    direct_match = re.search(function_pattern, text)
    if direct_match:
        start_pos = text.find(direct_match.group(0))
        if start_pos != -1:
            extracted_function = extract_complete_function(text, start_pos)
            return remove_comments(extracted_function)

    return ""


def test_extractor():
    """
    测试JavaScript函数提取函数
    """
    # 测试用例
    test_text = '''
    function generateChartOption(chartData) {
        // 1. 输入验证
        if (!chartData?.metadata?.columns || !chartData?.data) {
            throw new Error('Invalid chart data format');
        }
        // 2. 数据提取与转换
        const { columns } = chartData.metadata;
        const [record] = chartData.data; // 假设这里只有一个数据记录
        if (columns.length !== Object.keys(record).length) {
            throw new Error('Column count mismatch with record data');
        }
        let total_count_value = null;
        for (const column of columns) {
            const value = record[column];
            if (!value) continue; // 跳过空值
            total_count_value = parseFloat(value); // 转换为数值
            break;
        }
        // 3. 组装完整的 option 配置
        return {
            title: { text: '查询总数据量', textStyle: { fontSize: 16 } },
            tooltip: { formatter: '{a} <br/>{b}: {c}' }, // 按要求设置提示框格式
            series: [
                {
                    name: '总数据',
                    type: 'gauge',
                    center: ['50%', '75%'],
                    radius: '80%',
                    detail: {
                        formatter: '{value}',
                        fontSize: 14 // 设置字体大小
                    },
                    data: [{
                        value: total_count_value,
                        name: '总数据量'
                    }],
                    max: (total_count_value * 2) || 100 // 确保最大值合理设置, 如果没有有效数据则默认为100
                }
            ]
        };
    }
    '''

    print("测试提取并去除注释:")
    print(extract_js_function(test_text, "generateChartOption"))


# 运行测试
if __name__ == "__main__":
    test_extractor()