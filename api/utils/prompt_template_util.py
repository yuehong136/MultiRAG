import os
from string import Template
from typing import Any


class PromptTemplateUtil:
    """
    用于加载提示词模板并填充值的工具类。
    """

    @staticmethod
    def load_template_from_file(file_path: str) -> str:
        """
        从文件中加载提示词模板。

        参数:
            file_path: 模板文件路径

        返回:
            以字符串形式返回模板内容
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"模板文件未找到: {file_path}")

        with open(file_path, encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def fill_template(template: str, values: dict[str, Any]) -> str:
        """
        使用Python的string.Template填充模板中的占位符。

        支持$variable_name或${variable_name}格式的占位符

        参数:
            template: 包含占位符的模板字符串
            values: 用于填充模板的值字典

        返回:
            填充后的模板字符串
        """
        # 将所有值转换为字符串
        str_values = {k: str(v) for k, v in values.items()}

        # 使用Python内置的Template进行替换
        template_obj = Template(template)
        return template_obj.safe_substitute(str_values)

    @staticmethod
    def fill_template_with_format(template: str, values: dict[str, Any]) -> str:
        """
        使用str.format()和命名占位符的替代方法。

        支持{variable_name}格式的占位符

        参数:
            template: 包含占位符的模板字符串
            values: 用于填充模板的值字典

        返回:
            填充后的模板字符串
        """
        try:
            return template.format(**values)
        except KeyError:
            # 优雅地处理缺失的键，保留原始占位符不变
            return template
