# chat_factory.py
from core.llm.chat_model.models.doubao_chat import DoubaoChat
from core.llm.chat_model.models.ernie_chat import ErnieChat
from core.llm.chat_model.models.qwen_chat import QWenChat
from core.llm.chat_model.models.zhipu_chat import ZhipuChat
from core.llm.chat_model.models.gptturbo import GptTurbo
from core.llm.chat_model.models.ollama_chat import OllamaChat


class ChatFactory:
    """
    聊天机器人实例工厂类。

    该类根据提供的模型名称创建并返回相应的聊天机器人实例。
    支持的模型名称以"glm"开头，未来可以扩展支持更多类型的模型。

    参数:
    - key: 用于访问聊天模型的密钥。
    - model_name: 聊天模型的名称，以"glm"开头。
    - base_url: 可选参数，指定模型服务的基URL。
    """

    def __init__(self, key, model_name, base_url=None):
        """
        初始化ChatFactory实例。

        设置密钥、模型名称和基URL。
        """
        self.key = key
        self.model_name = model_name
        self.base_url = base_url

    def get_chat_instance(self):
        """
        根据模型名称创建并返回相应的聊天机器人实例。

        如果模型名称以"glm"开头，返回ZhipuChat实例，以此类推。
        如果模型名称不被支持，则抛出ValueError异常。

        返回:
        - 聊天机器人实例，具体类型取决于模型名称。
        """
        if self.model_name.startswith("glm"):
            # 根据模型名称创建并返回ZhipuChat实例
            return ZhipuChat(self.key, self.model_name, self.base_url)
        elif self.model_name.startswith("gpt"):
            return GptTurbo(self.key, self.model_name, self.base_url)
        elif self.model_name.endswith("b"):
            self.base_url = "http://localhost:6006"
            return OllamaChat(self.key, self.model_name, self.base_url)
        elif self.model_name.startswith("ep"):
            self.base_url = "https://ark.cn-beijing.volces.com/api/v3"
            return DoubaoChat(self.key, self.model_name, self.base_url)
        elif self.model_name.startswith("ERNIE"):
            return ErnieChat(self.key, self.model_name, self.base_url)
        elif self.model_name.startswith("qwen"):
            return QWenChat(self.key, self.model_name, self.base_url)
        # 可以添加更多模型的实例化条件
        else:
            # 如果模型名称不被支持，则抛出异常
            raise ValueError("Unsupported model name")
