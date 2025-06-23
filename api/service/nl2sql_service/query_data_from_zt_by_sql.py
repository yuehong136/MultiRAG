import base64
import aiohttp
import asyncio
import json
import random
import string
from typing import List, Any, Dict
from api.settings import DCS_SERVER_PROTOCOL, DCS_SERVER_HOST, DCS_SERVER_PORT


def generate_random_prefix(length: int = 5) -> str:
    """
    生成指定长度的随机字符串前缀

    参数:
        length (int): 随机字符串的长度，默认为5

    返回:
        str: 随机生成的字符串
    """
    # 使用字母和数字组合生成随机字符
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))


async def query_data_from_zt_by_sql(sql: str) -> dict:
    """
    发送异步非阻塞请求到AI数据查询接口

    参数:
        sql (str): 需要执行的SQL查询语句

    返回:
        dict: API响应的JSON对象
    """
    try:
        # 加密数据：转换为base64并在前面添加5个随机字符
        base64_data = base64.b64encode(sql.encode()).decode()
        random_prefix = generate_random_prefix()
        encrypted_params = f"{random_prefix}{base64_data}"

        # 准备请求URL和请求体
        base_url = f"{DCS_SERVER_PROTOCOL}://{DCS_SERVER_HOST}:{DCS_SERVER_PORT}"
        endpoint = "/api/cmai/appCenter/aiDataInquiry/nl2SqlData"
        url = f"{base_url}{endpoint}"

        payload = {
            "params": encrypted_params
        }

        # 发送异步请求
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                # 检查请求是否成功
                if response.status == 200:
                    result = await response.json()
                    return result["data"]
                else:
                    error_text = await response.text()
                    return {
                        "success": False,
                        "error": f"请求失败，状态码：{response.status}",
                        "error_details": error_text
                    }

    except Exception as e:
        return {
            "success": False,
            "error": f"发生异常：{str(e)}"
        }


async def query_data_with_params(sql: str, dataset_wid: int, sql_params: List[Any]) -> dict:
    """
    发送异步非阻塞请求到AI数据查询接口（参数化查询）

    参数:
        sql (str): 参数化SQL查询语句（包含?占位符）
        dataset_wid (int): 数据集ID
        sql_params (List[Any]): SQL参数列表，对应SQL中的?占位符

    返回:
        dict: API响应的JSON对象
    """
    try:
        # 准备参数对象
        param_data = {
            "sql": sql,
            "dataset_wid": dataset_wid,
            "sqlParams": sql_params
        }

        # 将参数对象转换为JSON字符串，然后进行base64编码
        json_str = json.dumps(param_data, ensure_ascii=False)
        base64_data = base64.b64encode(json_str.encode()).decode()
        random_prefix = generate_random_prefix()
        encrypted_params = f"{random_prefix}{base64_data}"

        # 准备请求URL和请求体
        base_url = f"{DCS_SERVER_PROTOCOL}://{DCS_SERVER_HOST}:{DCS_SERVER_PORT}"
        endpoint = "/api/cmai/appCenter/aiDataInquiry/nl2SqlDataParam"
        url = f"{base_url}{endpoint}"

        payload = {
            "params": encrypted_params
        }

        # 发送异步请求
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                # 检查请求是否成功
                if response.status == 200:
                    result = await response.json()
                    if result["code"] == "-1":
                        return {
                            "status": "error",
                            "message": result["msg"]
                        }
                    else:
                        return {
                            "status": "success",
                            "data": result["data"]
                        }
                else:
                    error_text = await response.text()
                    return {
                        "success": False,
                        "error": f"请求失败，状态码：{response.status}",
                        "error_details": error_text
                    }

    except Exception as e:
        return {
            "success": False,
            "error": f"发生异常：{str(e)}"
        }


# 使用示例
async def main():
    # 示例1：普通查询
    #     print("=== 示例1：普通查询 ===")
    #     query = """
    # -- [查询意图为按部门分组统计教师数量。主要内容是从教师信息表和高校部门信息表获取数据，按部门名称分组统计每个部门的教师人数。无时间范围，因非明细数据查询不涉及分页。]\nSELECT d.department_name, COUNT(t.teacher_id) AS teacher_count\nFROM gx_test_teachers t -- 教师信息表，别名为t\nLEFT JOIN gx_test_departments d -- 高校部门信息表，别名为d\nON t.department_id = d.department_id\nGROUP BY d.department_name;
    #     """
    #
    #     response1 = await query_data_from_zt_by_sql(query)
    #     print(json.dumps(response1, indent=2, ensure_ascii=False))

    # 示例2：参数化查询
    print("\n=== 示例2：参数化查询 ===")
    param_sql = """SELECT "t1"."teacher_id", "t1"."name", "t1"."title"
FROM gx_test_teachers AS t1
         LEFT JOIN gx_test_departments AS t2 ON t1.department_id = t2.department_id
WHERE "t2"."department_name" = ?
ORDER BY "t1"."gender" ASC"""
    dataset_wid = 35799132679879680
    sql_params = ["计算机科学与技术学院"]

    try:
        response2 = await query_data_with_params(param_sql, dataset_wid, sql_params)
        print(json.dumps(response2, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"发生错误: {e}")


# 如果直接执行此文件则运行示例
if __name__ == "__main__":
    asyncio.run(main())
