import os

from workflow.llm.basic.LLM import LLM


# 字节火山方舟
class VolcengineLLM(LLM):
    pass


if __name__ == "__main__":
    api_key = os.getenv("API_KEY")
    # 使用字节火山方舟LLM
    volcengine_llm = VolcengineLLM(api_key, model="ep-20240808173556-h7vxq", base_url="https://ark.cn-beijing.volces.com/api/v3")
    response = volcengine_llm.generate("讲个笑话")
    print(response)
