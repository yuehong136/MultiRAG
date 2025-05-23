import base64
import aiohttp
import asyncio
import json
from api.settings import DCS_SERVER_PROTOCOL, DCS_SERVER_HOST, DCS_SERVER_PORT


async def query_data_from_zt_by_sql(sql: str) -> dict:
    """
    发送异步非阻塞请求到AI数据查询接口

    参数:
        query_data (str): 需要加密的原始查询数据

    返回:
        dict: API响应的JSON对象
    """
    try:
        # 加密数据：转换为base64并在前面添加'abcde'
        base64_data = base64.b64encode(sql.encode()).decode()
        encrypted_params = f"abcde{base64_data}"

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


# 使用示例
async def main():
    # 示例查询数据
    query = """
-- [查询意图为按部门分组统计教师数量。主要内容是从教师信息表和高校部门信息表获取数据，按部门名称分组统计每个部门的教师人数。无时间范围，因非明细数据查询不涉及分页。]\nSELECT d.department_name, COUNT(t.teacher_id) AS teacher_count\nFROM gx_test_teachers t -- 教师信息表，别名为t\nLEFT JOIN gx_test_departments d -- 高校部门信息表，别名为d\nON t.department_id = d.department_id\nGROUP BY d.department_name;
    """

    # 发送请求并获取响应
    response = await query_data_from_zt_by_sql(query)

    # 打印响应结果
    print(json.dumps(response, indent=2, ensure_ascii=False))


# 如果直接执行此文件则运行示例
if __name__ == "__main__":
    asyncio.run(main())
