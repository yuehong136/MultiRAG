chart_type_templates = {
    "饼图": "pie_chart_template.txt",
    "柱状图": "bar_chart_template.txt",
    "折线图": "line_chart_template.txt",
    "仪表盘": "gauge_chart_template.txt",
}

import os


def load_chart_template(chart_type):
    # 获取当前脚本所在的目录
    current_dir = os.path.dirname(os.path.abspath(__file__))

    # 检查请求的图表类型是否存在
    if chart_type not in chart_type_templates:
        raise ValueError(f"Unsupported chart type: {chart_type}")

    # 构建模板文件的完整路径
    template_file = os.path.join(current_dir, "templates", chart_type_templates[chart_type])

    # 检查文件是否存在
    if not os.path.exists(template_file):
        raise FileNotFoundError(f"Template file not found: {template_file}")

    # 读取并返回模板内容
    try:
        with open(template_file, 'r', encoding='utf-8') as file:
            return file.read()
    except IOError as e:
        raise IOError(f"Error reading template file: {e}")


if __name__ == "__main__":
    # 使用示例
    template_content = load_chart_template("饼图")
    print(template_content)
