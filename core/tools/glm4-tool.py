from zhipuai import ZhipuAI

client = ZhipuAI(api_key="7ae32940233e38153d5ebaf94844f3e2.gwrz4P0tH9IDijUv")  # 请填写您自己的APIKey

tools = [
    {
        "type": "function",
        "function": {
            "name": "query_train_info",
            "description": "根据用户提供的信息，查询对应的车次",
            "parameters": {
                "type": "object",
                "properties": {
                    "departure": {
                        "type": "string",
                        "description": "出发城市或车站",
                    },
                    "destination": {
                        "type": "string",
                        "description": "目的地城市或车站",
                    },
                    "date": {
                        "type": "string",
                        "description": "要查询的车次日期",
                    },
                },
                "required": ["departure", "destination", "date"],
            },
        },
    }
]
messages = [{"role": "user", "content": "你能帮我查询2024年1月1日从北京南站到上海的火车票吗？"}]
response = client.chat.completions.create(
    model="glm-4",  # 填写需要调用的模型名称
    messages=messages,
    tools=tools,
    tool_choice="auto",
)
print(response.choices[0].message)
