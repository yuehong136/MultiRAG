import datetime
from typing import Any

import pandas as pd
import psycopg2


# TODO 仅开发时使用该文件来查询数据，后续需要通过中台接口进行数据查询
def connect_to_postgres(host: str, port: int, database: str, user: str, password: str) -> psycopg2.extensions.connection | None:
    """
    连接到PostgreSQL数据库

    参数:
        host: 数据库主机地址
        port: 数据库端口
        database: 数据库名称
        user: 用户名
        password: 密码

    返回:
        如果连接成功，返回连接对象；否则返回None
    """
    try:
        conn = psycopg2.connect(host=host, port=port, database=database, user=user, password=password)
        return conn
    except Exception as e:
        print(f"连接数据库时出错: {e}")
        return None


def execute_query(conn: psycopg2.extensions.connection, sql: str) -> pd.DataFrame | None:
    """
    执行SQL查询并返回结果

    参数:
        conn: 数据库连接对象
        sql: 要执行的SQL查询语句

    返回:
        如果查询成功，返回包含查询结果的DataFrame；否则返回None
    """
    try:
        # 使用pandas读取SQL查询结果
        df = pd.read_sql_query(sql, conn)
        return df
    except Exception as e:
        print(f"执行查询时出错: {e}")
        return None


def get_python_type_name(value):
    """
    获取Python值的类型名称

    参数:
        value: Python值

    返回:
        类型名称字符串
    """
    if value is None:
        return "null"
    elif isinstance(value, int):
        return "integer"
    elif isinstance(value, float):
        return "float"
    elif isinstance(value, str):
        return "string"
    elif isinstance(value, bool):
        return "boolean"
    elif isinstance(value, (datetime.date, datetime.datetime)):
        return "date"
    elif isinstance(value, list):
        return "array"
    elif isinstance(value, dict):
        return "object"
    else:
        # 返回Python类型名
        return type(value).__name__


def infer_column_types(df: pd.DataFrame) -> list[dict[str, str]]:
    """
    从DataFrame推断列类型

    参数:
        df: 包含数据的DataFrame

    返回:
        包含列名和数据类型的字典列表
    """
    column_and_type = []

    for column in df.columns:
        # 获取非空值
        non_null_values = df[column].dropna()

        if len(non_null_values) > 0:
            # 使用第一个非空值来确定类型
            sample_value = non_null_values.iloc[0]
            type_name = get_python_type_name(sample_value)
        else:
            # 如果列全是空值，则使用pandas dtype
            pandas_type = str(df[column].dtype)
            if "int" in pandas_type:
                type_name = "integer"
            elif "float" in pandas_type:
                type_name = "float"
            elif "bool" in pandas_type:
                type_name = "boolean"
            elif "datetime" in pandas_type:
                type_name = "date"
            else:
                type_name = "string"  # 默认为字符串类型

        column_and_type.append({"column_name": column, "type": type_name})

    return column_and_type


def execute_sql_and_format_result(db_config: dict[str, Any] | None, sql: str) -> dict[str, Any]:
    """
    连接PostgreSQL数据库并执行查询，返回完整的查询结果，并格式化为指定格式

    参数:
        db_config: 包含数据库连接信息的字典
        sql: 要执行的SQL查询语句

    返回:
        包含查询结果的字典，格式为 {"column_and_type": [...], "sql_result": {"columns": [...], "data": [...]}}
    """
    if db_config is None:
        db_config = {"host": "122.112.237.137", "port": 5432, "database": "postgres", "user": "usr_data", "password": "Dtv123546@dev"}

    # 从配置中获取连接信息
    host = db_config.get("host", "122.112.237.137")
    port = db_config.get("port", 5432)
    database = db_config.get("database", "postgres")
    user = db_config.get("user", "usr_data")
    password = db_config.get("password", "Dtv123546@dev")

    # 连接数据库
    conn = connect_to_postgres(host, port, database, user, password)

    if conn is None:
        return {"error": "无法连接到数据库"}

    try:
        # 执行查询获取完整数据
        df = execute_query(conn, sql)

        if df is None:
            return {"column_and_type": [], "sql_result": {"columns": [], "data": []}, "error": "查询执行失败"}

        # 从数据中推断列类型
        column_and_type = infer_column_types(df)

        # 准备返回数据
        columns = list(df.columns)

        # 将DataFrame转换为所需的列表格式
        data = []
        for _, row in df.iterrows():
            row_dict = {}
            for col in columns:
                value = row[col]
                # 处理特殊类型
                if pd.isna(value):
                    row_dict[col] = None
                elif isinstance(value, (datetime.date, datetime.datetime)):
                    row_dict[col] = value.strftime("%Y-%m-%d")
                else:
                    row_dict[col] = value
            data.append(row_dict)

        # 构建返回结果
        result = {"column_and_type": column_and_type, "sql_result": {"columns": columns, "data": data}}

        return result
    finally:
        # 确保连接关闭
        conn.close()
        print("数据库连接已关闭")


# 使用示例
if __name__ == "__main__":
    # 数据库连接配置
    db_config = {
        "host": "122.112.237.137",  # 数据库主机地址
        "port": 5432,  # 端口
        "database": "postgres",  # 数据库名称
        "user": "usr_data",  # 用户名
        "password": "Dtv123546@dev",  # 密码
    }

    # 示例SQL查询
    sql_query = """
    SELECT t.teacher_id, t.name, t.title, d.department_name\nFROM gx_test_teachers t\nLEFT JOIN gx_test_departments d ON t.department_id = d.department_id\nWHERE d.department_name = '计算机科学与技术学院' AND t.title = '副教授';
    """

    # 执行查询，获取格式化结果
    result = execute_sql_and_format_result(db_config, sql_query)

    print("查询结果:")
    print(f"列信息: {result['column_and_type']}")
    print(f"列: {result['sql_result']['columns']}")
    print(f"数据: {result['sql_result']['data']}")
    print(f"样例数据列: {result['sql_result']['data'][0:2]}")
