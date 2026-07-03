import os
from typing import Any

from pydantic import BaseModel


class PromptTemplateLoader:
    def __init__(self):
        """
        初始化 PromptTemplateLoader。
        """
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.template_dir = os.path.join(current_dir, "templates")

    def load_template(self, template_name: str) -> str:
        """
        从文件加载模板。

        :param template_name: 模板文件名（包括扩展名）
        :return: 加载的模板字符串
        """
        template_path = os.path.join(self.template_dir, template_name)
        with open(template_path, encoding='utf-8') as file:
            return file.read()

    def fill_template(self, template_name: str, data: Any | None = None, **kwargs) -> str:
        """
        加载模板并填充值。支持Pydantic模型、字典和关键字参数。

        :param template_name: 模板文件名（包括扩展名）
        :param data: 用于填充模板的数据，可以是Pydantic模型或字典（可选）
        :param kwargs: 额外的关键字参数
        :return: 填充后的字符串
        """
        from string import Template
        template_string = self.load_template(template_name)
        template = Template(template_string)

        # 初始化一个空字典来存储所有的键值对
        fill_data = {}

        # 处理data参数
        if data is not None:
            if isinstance(data, BaseModel):
                fill_data.update(data.dict())
            elif isinstance(data, dict):
                fill_data.update(data)
            else:
                raise ValueError("Data must be either a Pydantic model or a dictionary")

        # 添加kwargs中的键值对，这些值会覆盖data中的同名键
        fill_data.update(kwargs)

        return template.safe_substitute(fill_data)


# 使用示例
if __name__ == "__main__":
    from pydantic import BaseModel


    class NL2SQLReqBody(BaseModel):
        user_question: str
        table_structure: str


    loader = PromptTemplateLoader()

    # 创建一个NL2SQLReqBody实例
    req_body = NL2SQLReqBody(
        user_question="查找所有年龄大于30岁的用户",
        table_structure="表名：user\n列：id (int), name (varchar), age (int)"
    )

    # 使用Pydantic模型填充模板
    filled_prompt1 = loader.fill_template("nl2sql_temp.txt", req_body)
    print("使用Pydantic模型：")
    print(filled_prompt1)

    # 使用字典填充模板
    data_dict = {
        "user_question": "列出所有用户名",
        "table_structure": "表名：user\n列：id (int), name (varchar), age (int)"
    }
    filled_prompt2 = loader.fill_template("nl2sql_temp.txt", data_dict)
    print("\n使用字典：")
    print(filled_prompt2)

    # 使用关键字参数填充模板
    filled_prompt3 = loader.fill_template("nl2sql_temp.txt",
                                          user_question="查找年龄最大的用户",
                                          table_structure="表名：user\n列：id (int), name (varchar), age (int)")
    print("\n使用关键字参数：")
    print(filled_prompt3)

    # 混合使用Pydantic模型和关键字参数
    filled_prompt4 = loader.fill_template("nl2sql_temp.txt", req_body, user_question="查找年龄最小的用户")
    print("\n混合使用Pydantic模型和关键字参数：")
    print(filled_prompt4)
