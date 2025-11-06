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
#         print("=== 示例1：普通查询 ===")
#         query = """
#         SELECT COUNT(*) AS total_teachers
# FROM gx_test_teachers t1
# WHERE "t1"."hire_date" >= CAST('2009-01-01' AS DATE)
#   AND "t1"."hire_date" <= CAST('2010-12-31' AS DATE)
#         """
#
#         response1 = await query_data_from_zt_by_sql(query)
#         print(json.dumps(response1, indent=2, ensure_ascii=False))

    # 示例2：参数化查询
    print("\n=== 示例2：参数化查询 ===")
    param_sql = """EXPLAIN SELECT t1."‘cjgzny" AS "参加工作年月", t1."csrq" AS "出生日期", t1."jg" AS "籍贯", t1."jgh" AS "教工号", t1."lxrq" AS "来校日期", t1."ryflmc" AS "人员分类名称", t1."xm" AS "姓名", t1."xwdm" AS "学位代码", t1."xbm" AS "性别码", t1."zzmmdm" AS "政治面貌代码", t2."bm" AS "部门", t2."cjdprq" AS "参加党派日期", t2."zzmm" AS "政治面貌" FROM t_jzg t1 LEFT JOIN t_jzg_zzmml t2 ON t2."gh" = t1."jgh";"""
    dataset_wid = 38608841318432768
    sql_params = []

    try:
        response2 = await query_data_with_params(param_sql, dataset_wid, sql_params)
        print(json.dumps(response2, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"发生错误: {e}")


# 如果直接执行此文件则运行示例
if __name__ == "__main__":
    asyncio.run(main())
