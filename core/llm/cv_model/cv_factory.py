"""
@project: multirag
@Author：龙
@file： cv_factory.py
@date：2024/7/9 9:00
@desc:
"""
from core.llm.cv_model.models.zhipu_4v import Zhipu4V


class CVModelFactory:
    """
    计算机视觉模型实例工厂类。

    该类根据提供的模型名称创建并返回相应的计算机视觉模型实例。
    支持的模型名称以特定前缀或后缀区分，未来可以扩展支持更多类型的模型。

    参数:
    - key: 用于访问模型的密钥。
    - model_name: 模型的名称。
    - base_url: 可选参数，指定模型服务的基URL。
    """

    def __init__(self, key, model_name, base_url=None):
        """
        初始化CVModelFactory实例。

        设置密钥、模型名称和基URL。
        """
        self.key = key
        self.model_name = model_name
        self.base_url = base_url


    @staticmethod
    def get_model_instance(key, model_name, lang="Chinese", **kwargs):
        """
        根据模型名称创建并返回相应的计算机视觉模型实例。

        如果模型名称符合特定前缀或后缀条件，返回对应模型实例。
        如果模型名称不被支持，则抛出ValueError异常。

        返回:
        - 计算机视觉模型实例，具体类型取决于模型名称。
        """
        if model_name.startswith("glm"):
            # 返回 ResNet 模型实例
            return Zhipu4V(key, model_name, lang, **kwargs)
        # 可以添加更多模型的实例化条件
        else:
            # 如果模型名称不被支持，则抛出异常
            raise ValueError("Unsupported model name")
