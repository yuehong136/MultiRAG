import json

from core.llm import OpenAIEmbed


class VolcEngineEmbed(OpenAIEmbed):
    def __init__(self, key, model_name, base_url="https://ark.cn-beijing.volces.com/api/v3"):
        if not base_url:
            base_url = "https://ark.cn-beijing.volces.com/api/v3"
        ark_api_key = json.loads(key).get('ark_api_key', '')
        model_name = json.loads(key).get('ep_id', '') + json.loads(key).get('endpoint_id', '')
        super().__init__(ark_api_key,model_name,base_url)