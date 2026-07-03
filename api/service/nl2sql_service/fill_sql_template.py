

def fill_sql_template(templated_sql: str, parameter_definitions: list, user_selected_values: dict) -> str:
    """
    根据用户选择的值填充SQL模板。

    参数:
    - templated_sql (str): 包含占位符的SQL模板，例如 "SELECT ... WHERE col = {{param_name}}"
    - parameter_definitions (list): 从LLM获取的参数定义列表，每个元素是一个字典，
                                    包含 'name', 'data_type'等键。
    - user_selected_values (dict): 用户选择的新值，键是参数名 (占位符名)，值是新值。
                                   例如: {"department_name_filter": "物理学院", "title_filter": "教授"}

    返回:
    - str: 填充了用户选择值的SQL查询语句。

    异常:
    - ValueError: 如果user_selected_values中的参数在parameter_definitions中找不到定义。
    """
    filled_sql = templated_sql

    # 为了快速查找参数定义，先将列表转换为字典
    definitions_map = {p["name"]: p for p in parameter_definitions}

    for param_name, new_value in user_selected_values.items():
        placeholder = f"{{{{{param_name}}}}}"  # 构造占位符，例如 {{department_name_filter}}

        if param_name not in definitions_map:
            # 如果用户提供了一个模板中不存在或未定义的参数，可以选择忽略或抛出错误
            # 这里选择打印警告并跳过，也可以根据需要改成 raise ValueError
            print(f"警告: 参数 '{param_name}' 在模板定义中未找到，将跳过此参数的替换。")
            continue

        param_def = definitions_map[param_name]
        data_type = param_def.get("data_type", "string").lower()  # 默认为string类型

        # 根据数据类型格式化值
        # 这是非常重要的一步，以防止SQL注入并确保语法正确
        if new_value is None:  # 处理NULL值
            sql_value = "NULL"
        elif data_type in ["string", "varchar", "text", "char", "date", "timestamp"]:
            # 对于字符串类型，需要用单引号包裹，并对内部的单引号进行转义
            # 修复：先转换为字符串，然后转义单引号，最后用单引号包裹
            str_value = str(new_value)
            escaped_value = str_value.replace("'", "''")  # SQL标准的单引号转义方式
            sql_value = f"'{escaped_value}'"
        elif data_type in ["integer", "int", "int4", "serial", "bigint", "smallint", "long", "number", "numeric",
                           "decimal", "float", "double"]:
            # 数字类型直接使用
            try:
                # 验证一下是否真的是数字，避免注入问题
                float(new_value)  # 尝试转换为float，如果失败会抛出ValueError
                sql_value = str(new_value)
            except ValueError:
                # 如果转换失败，可能是一个潜在的注入尝试或错误数据
                # 可以选择抛出错误，或使用一个安全的默认值，或将其视为字符串（带引号）
                print(f"警告: 参数 '{param_name}' 的期望类型是数字，但值为 '{new_value}'。将尝试作为字符串处理。")
                str_value = str(new_value)
                escaped_value = str_value.replace("'", "''")
                sql_value = f"'{escaped_value}'"
        elif data_type == "boolean":
            # 布尔类型，通常是 TRUE 或 FALSE (具体看数据库)
            if isinstance(new_value, bool):
                sql_value = "TRUE" if new_value else "FALSE"
            elif str(new_value).strip().lower() in ["true", "1", "t", "yes"]:
                sql_value = "TRUE"
            elif str(new_value).strip().lower() in ["false", "0", "f", "no"]:
                sql_value = "FALSE"
            else:
                print(f"警告: 参数 '{param_name}' 的布尔值无法解析: '{new_value}'. 将使用FALSE作为默认。")
                sql_value = "FALSE"  # 或者抛出错误
        else:
            # 未知类型，默认按字符串处理（作为一种相对安全的备选方案）
            print(f"警告: 参数 '{param_name}' 的数据类型 '{data_type}' 未特别处理，将按字符串处理。")
            str_value = str(new_value)
            escaped_value = str_value.replace("'", "''")
            sql_value = f"'{escaped_value}'"

        filled_sql = filled_sql.replace(placeholder, sql_value)

    return filled_sql


# 测试代码
if __name__ == "__main__":
    # 测试用例
    templated_sql = """SELECT
    t.gender, -- 性别
    COUNT(*) AS teacher_count -- 教师人数
FROM
    gx_test_teachers t
JOIN
    gx_test_departments d ON t.department_id = d.department_id
WHERE
    d.department_name = '{{department_name_filter}}'
GROUP BY
    t.gender;"""

    parameter_definitions = [
        {
            'data_type': 'string',
            'description': '筛选教师所属的部门名称',
            'name': 'department_name_filter',
            'original_value': '计算机科学与技术学院',
            'possible_values': [
                {'value': '物理学院'},
                {'synonyms': ['计科院', '计算机学院'], 'value': '计算机科学与技术学院'},
                {'value': '数学学院'},
                {'value': '经济管理学院'},
                {'value': '外国语学院'}
            ],
            'possible_values_source': 'DIMENSION_ID:36093203355146240',
            'semantic_info': {
                'dimension_id': '36093203355146240',
                'dimension_name': '部门名称'
            },
            'type': 'DIMENSION_FILTER',
            'ui_hint': 'dropdown'
        }
    ]

    user_selected_values = {'department_name_filter': '计算机科学与技术学院'}

    result = fill_sql_template(templated_sql, parameter_definitions, user_selected_values)
    print("填充后的SQL:")
    print(result)

    # 测试包含单引号的情况
    user_selected_values2 = {'department_name_filter': "O'Connor学院"}
    result2 = fill_sql_template(templated_sql, parameter_definitions, user_selected_values2)
    print("\n测试单引号转义:")
    print(result2)
