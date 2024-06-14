import openai
from core.components.llm import zhipuai
class Action:
    def __init__(self, api_key):
        openai.api_key = api_key

    def execute(self, plan):
        if "生成 SQL 语句" in plan:
            # response = openai.Completion.create(
            #     model="gpt-3.5-turbo",
            #     prompt="根据用户输入生成 SQL 语句",
            #     max_tokens=150
            # )
            response = zhipuai(messages=[{'role':'user','content':plan}],
                                temperature=0.7,
                                max_tokens=150,
                                sys_prompt="根据用户输入生成 SQL 语句",api_token="7ae32940233e38153d5ebaf94844f3e2.gwrz4P0tH9IDijUv")
            return response
        else:
            return "无法执行非 SQL 相关任务"
