import json

from core.llm.chat_model.base import Base


class VolcEngineChat(Base):
    def __init__(self, key, model_name, base_url="https://ark.cn-beijing.volces.com/api/v3"):
        super().__init__(key, model_name, base_url=None)

        """
        Since do not want to modify the original database fields, and the VolcEngine authentication method is quite special,
        Assemble ark_api_key, ep_id into api_key, store it as a dictionary type, and parse it for use
        model_name is for display only
        """
        base_url = base_url if base_url else "https://ark.cn-beijing.volces.com/api/v3"
        ark_api_key = json.loads(key).get("ark_api_key", "")
        model_name = json.loads(key).get("ep_id", "") + json.loads(key).get("endpoint_id", "")
        super().__init__(ark_api_key, model_name, base_url)