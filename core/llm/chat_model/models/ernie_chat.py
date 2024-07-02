# ernie_chat.py
import os
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
from core.llm.chat_model.base import Base
import qianfan

@dataclass
class ErnieChat(Base):
    key: str
    model_name: str
    base_url: Optional[str] = None
    client: qianfan.ChatCompletion = field(init=False)

    def __post_init__(self):
        # 设置环境变量，用于安全认证
        # os.environ["QIANFAN_ACCESS_KEY"] = self.key
        # os.environ["QIANFAN_SECRET_KEY"] = "MGgnTdViQVnfLi36ePvSQbMEnevLCsu4"  # 这里需要替换成实际的Secret Key
        self.client = qianfan.ChatCompletion(ak=self.key, sk="3B2ATvam5xi2QW6UrKZpy3hWh1wUJnCy")
        print(f"Qianfan客户端已初始化，使用Key: {self.key}")

    def chat(self, system: str, history: List[Dict[str, Any]], gen_conf: Dict[str, Any]) -> Tuple[str, int]:
        # 如果有系统消息，添加到历史记录的开头
        if system:
            gen_conf["system"] = system
            history.append({"role": "user", "content": '根据用户提问和聊天的内容，请你自行结合场景生成3个推荐可以继续聊天的话题，话题请按照如下格式为返回：["xxx","xxx","xxx"]，仅返回要求返回的内容'})
        # try:
        # 发送请求并获取响应
        response = self.client.do(
            model=self.model_name,
            messages=history,
            **gen_conf
        )
        # print(response)
        if isinstance(response, qianfan.QfResponse) and hasattr(response, 'body'):
            ans = response.body.get("result", "").strip()
            return ans, len(history)
        else:
            return f"**错误**: Unexpected response format: {response}", 0
        # except Exception as e:
        #     return f"**错误**: {str(e)}", 0

    def chat_streamly(self, system: str, history: List[Dict[str, Any]], gen_conf: Dict[str, Any]):
        # 如果有系统消息，添加到历史记录的开头
        if system:
            gen_conf["system"] = system
            # print(history)
        try:
            # 发送流式请求并逐步处理响应
            response = self.client.do(
                model=self.model_name,
                messages=history,
                stream=True,
                **gen_conf
            )
            ans = ""
            for chunk in response:
                if isinstance(chunk, qianfan.QfResponse) and hasattr(chunk, 'body'):
                    chunk_body = chunk.body.get("result", "")
                    ans += str(chunk_body)
                    yield ans
                else:
                    yield f"**错误**: Unexpected response chunk format: {chunk}"
        except Exception as e:
            yield f"**错误**: {str(e)}"
